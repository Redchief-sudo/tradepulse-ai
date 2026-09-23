"""Receipt-backed crypto quantity fees. All financial writes share one transaction.

CFEE activity IDs are the authority; no rate or fill-to-fee relationship is
inferred. Asset units are allocated using the existing FIFO inventory policy.
Complete instrument populations permit atomic historical restatement of derived closures.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tradepulse.broker.symbols import normalize_alpaca_symbol
from tradepulse.models import AssetClass, AssetIdentity, ReconciliationOutcome, ReconciliationRecord, asset_identity_key
from tradepulse.models.base import decimal_value, require_text
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.time import aware_utc

logger = logging.getLogger(__name__)


class AssetFeeIntegrityError(ValueError):
    """No financial mutation committed; authoritative evidence needs review."""


class AssetFeePending(AssetFeeIntegrityError):
    """Let in-flight settlement complete before comparing or applying fees."""


@dataclass(frozen=True)
class AssetFee:
    activity_id: str
    asset: AssetIdentity
    quantity: Decimal
    occurred_at: datetime
    receipt: Mapping[str, Any]


def parse_asset_fee(raw: Mapping[str, Any]) -> AssetFee:
    if (raw.get('activity_type') != 'CFEE' or raw.get('status') != 'executed'
            or raw.get('description') != 'Coin Pair Transaction Fee (Non USD)'):
        raise AssetFeeIntegrityError('ASSET_FEE_UNSUPPORTED_RECEIPT')
    activity_id = require_text(raw.get('id'), 'asset_fee_id')
    if isinstance(raw['qty'], float) or isinstance(raw['net_amount'], float):
        raise AssetFeeIntegrityError('ASSET_FEE_INEXACT_NUMERIC_RECEIPT')
    quantity = -decimal_value(raw['qty'], 'asset_fee_quantity')
    if quantity <= 0 or decimal_value(raw['net_amount'], 'asset_fee_net_amount') != 0:
        raise AssetFeeIntegrityError('ASSET_FEE_UNSUPPORTED_DEBIT')
    symbol = normalize_alpaca_symbol(require_text(raw.get('symbol'), 'asset_fee_symbol'), AssetClass.CRYPTO)
    if len(symbol.split('/')) != 2 or not all(symbol.split('/')) or symbol.split('/')[1] != 'USD':
        raise AssetFeeIntegrityError('ASSET_FEE_IDENTITY_INVALID')
    occurred_at = aware_utc(raw.get('created_at'), field_name='asset_fee_created_at')
    return AssetFee(activity_id, AssetIdentity(symbol, AssetClass.CRYPTO, f'alpaca:{symbol}'), quantity, occurred_at, dict(raw))


async def reconcile_asset_fees(repositories, broker, *, now: datetime, lease_lost=None,
                               clock=lambda: datetime.now(UTC)) -> bool:
    """Called by reconciliation, never by a dashboard read or order submission."""
    lots = [hydrate('position_lots', row['payload']) for row in await paginate_all_rows(repositories.position_lots)]
    crypto_lots = [lot for lot in lots if lot.asset.asset_class == AssetClass.CRYPTO]
    if not crypto_lots:
        return True
    subject = 'asset_fee_feed'
    if lease_lost is not None and lease_lost.is_set():
        return False
    from tradepulse.models.market import asset_key_from_broker_symbol

    from .activity_cursor import activity_population
    from .epochs import block_asset
    from .fee_replay import replay_asset_fees
    assets = {asset_identity_key(lot.asset): lot.asset for lot in crypto_lots}
    success = True
    try:
        positions = await broker.get_positions()
        quantities = {}
        for position in positions:
            key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
            if key in quantities:
                raise AssetFeeIntegrityError('ASSET_FEE_DUPLICATE_BROKER_POSITION')
            quantities[key] = position.qty
    except Exception as exc:  # noqa: BLE001 - worker must survive provider failures
        for asset in assets.values():
            await block_asset(repositories, asset, str(exc), now=now)
        logger.warning('asset_fee_positions_unavailable', extra={'reason': str(exc)})
        return False
    for key, asset in sorted(assets.items()):
        if lease_lost is not None and lease_lost.is_set():
            return False
        try:
            cursor = await repositories.broker_activity_cursors.get(key)
            boundary = cursor['payload']['last_complete_activity_id'] if cursor else None
            raw, pagination = await activity_population(broker, boundary)
            now = aware_utc(clock(), field_name='asset_fee_population_observed_at')
            await replay_asset_fees(repositories, asset, raw, quantities.get(key, Decimal(0)),
                                    now=now, lease_lost=lease_lost, pagination=pagination)
            record = ReconciliationRecord(str(uuid4()), 'asset_fee', subject+':'+key, ReconciliationOutcome.MATCHED,
                                          expected={'receipt_verified': True},
                                          actual={'activities_checked': len(raw)}, occurred_at=now)
        except Exception as exc:  # noqa: BLE001 - one instrument must not kill another lane
            success = False
            await block_asset(repositories, asset, str(exc), now=now, pending=isinstance(exc, AssetFeePending))
            record = ReconciliationRecord(str(uuid4()), 'asset_fee', subject+':'+key, ReconciliationOutcome.DRIFT_DETECTED,
                                          expected={'receipt_verified': True}, actual={'error': str(exc)}, occurred_at=now)
            logger.warning('asset_fee_reconciliation_failed', extra={'asset_key': key, 'reason': str(exc)})
        await _record_feed_transition(repositories, record)
    return success


async def _record_feed_transition(repositories, record: ReconciliationRecord) -> None:
    """Append immutable feed evidence only when its outcome/content changes.

    Keep the failure -> recovery -> failure history, including repeated states
    after a transition. Unchanged polls create no rows. The comparison and
    insert share a transaction so concurrent polls cannot duplicate evidence.
    """
    def write(connection):
        row = connection.execute(
            "SELECT payload FROM reconciliation_records WHERE "
            "json_extract(payload, '$.reconciliation_type')=? "
            "AND json_extract(payload, '$.subject_id')=? "
            "ORDER BY rowid DESC LIMIT 1", (record.reconciliation_type, record.subject_id)
        ).fetchone()
        current = replace(record, corrective_action=record.reconciliation_type+'_observation')
        payload = decode_payload(encode_payload(current))
        if row is not None:
            previous = decode_payload(row['payload'])
            # The transactional replay already verified the immutable receipt and lots.
            # A successful ledger needs no extra replay row; a later failure
            # still receives a distinct recovery transition on this subject.
            if (record.outcome == ReconciliationOutcome.MATCHED
                    and 'ledger_record_id' in record.actual
                    and previous['outcome'] in ('corrected', 'matched')):
                return
            if all(previous.get(k) == payload.get(k) for k in ('subject_id', 'outcome', 'expected', 'actual')):
                return
        connection.execute(
            'INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
            (current.record_id, encode_payload(current), current.occurred_at.isoformat()),
        )
    await repositories.reconciliation_records.database.run(write, write=True)
