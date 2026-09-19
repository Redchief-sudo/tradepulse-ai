"""Receipt-backed crypto quantity fees. All financial writes share one transaction.

CFEE activity IDs are the authority; no rate or fill-to-fee relationship is
inferred. Asset units are allocated using the existing FIFO inventory policy.
Late events that would require restating completed closures fail closed.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tradepulse.broker.symbols import normalize_alpaca_symbol
from tradepulse.models import AssetClass, AssetIdentity, ReconciliationOutcome, ReconciliationRecord, asset_identity_key
from tradepulse.models.base import decimal_value, require_aware, require_text
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.risk import latch_financial_integrity_block

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
    occurred_at = require_aware(datetime.fromisoformat(raw['created_at']), 'asset_fee_created_at')
    return AssetFee(activity_id, AssetIdentity(symbol, AssetClass.CRYPTO, f'alpaca:{symbol}'), quantity, occurred_at, dict(raw))


async def apply_asset_fee(repositories: PersistenceRepositories, fee: AssetFee, *, now: datetime) -> bool:
    """Atomic receipt, lots, and holding update; false means verified replay.

    Existing fills and settlements are read-only. No cash movement, synthetic
    fill, or guessed fee rate is created. Cost-basis debit is recorded explicitly.
    """
    if parse_asset_fee(fee.receipt) != fee:
        raise AssetFeeIntegrityError('ASSET_FEE_RECEIPT_FIELDS_MISMATCH')
    record_id = f'asset_fee:{fee.activity_id}'
    key = asset_identity_key(fee.asset)
    require_aware(now, 'fee_observed_at')
    if fee.occurred_at > now:
        raise AssetFeeIntegrityError('ASSET_FEE_FUTURE_TIMESTAMP')

    def apply(connection: sqlite3.Connection) -> bool:
        previous = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?', (record_id,)).fetchone()
        lots = [hydrate('position_lots', decode_payload(row['payload'])) for row in connection.execute('SELECT payload FROM position_lots')]
        lots = [lot for lot in lots if asset_identity_key(lot.asset) == key]
        if previous is not None:
            payload = decode_payload(previous['payload'])
            if payload['actual'].get('activity') != dict(fee.receipt):
                raise AssetFeeIntegrityError('ASSET_FEE_RECEIPT_CHANGED')
            allocations = {lot.lot_id: lot.asset_fee_quantities[fee.activity_id] for lot in lots if fee.activity_id in lot.asset_fee_quantities}
            recorded = {k: decimal_value(v, 'fee_allocation', positive=True) for k, v in payload['actual']['allocations'].items()}
            basis = sum((allocations.get(lot.lot_id, Decimal(0))*lot.acquisition_price for lot in lots), Decimal(0))
            if (allocations != recorded or sum(allocations.values(), Decimal(0)) != fee.quantity
                    or basis != decimal_value(payload['actual']['cost_basis_debit'], 'fee_basis')):
                raise AssetFeeIntegrityError('ASSET_FEE_LEDGER_LOT_MISMATCH')
            return False
        if not lots:
            raise AssetFeeIntegrityError('ASSET_FEE_NO_LOCAL_LOTS')
        for row in connection.execute('SELECT payload FROM settlements'):
            event = hydrate('settlements', decode_payload(row['payload']))
            if asset_identity_key(event.asset) == key and event.status.value != 'completed':
                raise AssetFeePending('ASSET_FEE_SETTLEMENT_PENDING')
        for row in connection.execute('SELECT payload FROM reconciliation_records'):
            prior = decode_payload(row['payload'])
            if prior['reconciliation_type'] != 'asset_fee' or prior['outcome'] != 'corrected':
                continue
            if prior['actual'].get('asset_key') == key:
                prior_time = datetime.fromisoformat(prior['actual']['activity']['created_at'])
                if prior_time > fee.occurred_at:
                    raise AssetFeeIntegrityError('ASSET_FEE_HISTORICAL_REPLAY_REQUIRED')
        for lot in lots:
            opening = connection.execute('SELECT payload FROM fills WHERE record_id=?', (lot.originating_fill_id,)).fetchone()
            settled = connection.execute('SELECT payload FROM settlements WHERE fill_id=?', (lot.originating_fill_id,)).fetchone()
            if opening is None or settled is None:
                raise AssetFeeIntegrityError('ASSET_FEE_OPENING_EVIDENCE_MISSING')
            fill = hydrate('fills', decode_payload(opening['payload']))
            event = hydrate('settlements', decode_payload(settled['payload']))
            if (asset_identity_key(fill.asset) != key or asset_identity_key(event.asset) != key
                    or not event.integrity_verified or not event.lot_projected or not event.holding_projected):
                raise AssetFeeIntegrityError('ASSET_FEE_OPENING_EVIDENCE_INVALID')
            if event.broker_order_id and connection.execute('SELECT 1 FROM integrity_holds WHERE record_id=?', (event.broker_order_id,)).fetchone():
                raise AssetFeePending('ASSET_FEE_ORDER_VERIFICATION_PENDING')
            if lot.position_side != 'long':
                raise AssetFeeIntegrityError('ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY')
            if lot.opened_at > fee.occurred_at:
                continue
            for fill_id in lot.closures:
                row = connection.execute('SELECT payload FROM fills WHERE record_id=?', (fill_id,)).fetchone()
                if row is None or hydrate('fills', decode_payload(row['payload'])).filled_at >= fee.occurred_at:
                    raise AssetFeeIntegrityError('ASSET_FEE_HISTORICAL_REPLAY_REQUIRED')
        holding_row = connection.execute('SELECT payload FROM holdings WHERE record_id=?', (key,)).fetchone()
        if holding_row is None:
            raise AssetFeeIntegrityError('ASSET_FEE_HOLDING_MISSING')
        holding = hydrate('holdings', decode_payload(holding_row['payload']))
        if holding.quantity != sum((lot.signed_quantity for lot in lots), Decimal(0)):
            raise AssetFeeIntegrityError('ASSET_FEE_HOLDING_LOT_MISMATCH')
        remaining = fee.quantity
        changed = {}
        allocations = {}
        basis = Decimal(0)
        for lot in sorted(lots, key=lambda item: (item.opened_at, item.lot_id)):
            if remaining == 0:
                break
            if lot.opened_at > fee.occurred_at or lot.remaining_quantity == 0:
                continue
            amount = min(remaining, lot.remaining_quantity)
            changed[lot.lot_id] = replace(lot, remaining_quantity=lot.remaining_quantity-amount,
                                         asset_fee_quantities={**lot.asset_fee_quantities, fee.activity_id: amount})
            allocations[lot.lot_id] = amount
            basis += amount * lot.acquisition_price
            remaining -= amount
        if remaining != 0:
            raise AssetFeeIntegrityError('ASSET_FEE_INSUFFICIENT_HISTORICAL_INVENTORY')
        current_lots = [changed.get(lot.lot_id, lot) for lot in lots]
        quantity = sum((lot.remaining_quantity for lot in current_lots), Decimal(0))
        stamp = now.isoformat()
        for lot in changed.values():
            connection.execute('UPDATE position_lots SET payload=?, updated_at=? WHERE record_id=?', (encode_payload(lot), stamp, lot.lot_id))
        if quantity == 0:
            connection.execute('DELETE FROM holdings WHERE record_id=?', (key,))
        else:
            cost = sum((lot.remaining_quantity*lot.acquisition_price for lot in current_lots), Decimal(0))
            updated = replace(holding, quantity=quantity, average_price=cost/quantity, updated_at=now)
            connection.execute('UPDATE holdings SET payload=?, updated_at=? WHERE record_id=?', (encode_payload(updated), stamp, key))
        record = ReconciliationRecord(
            record_id, 'asset_fee', fee.activity_id, ReconciliationOutcome.CORRECTED,
            expected={'quantity_debit': fee.quantity},
            actual={'activity': fee.receipt, 'asset_key': key, 'allocations': allocations,
                    'cost_basis_debit': basis, 'holding_before': holding.quantity, 'holding_after': quantity},
            occurred_at=now, corrective_action='applied authoritative asset-unit fee once using FIFO; lots and holding committed atomically',
        )
        connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (record_id, encode_payload(record), stamp))
        return True

    return await repositories.position_lots.database.run(apply, write=True)


async def reconcile_asset_fees(repositories, broker, *, now: datetime, lease_lost=None) -> bool:
    """Called by reconciliation, never by a dashboard read or order submission."""
    lots = [hydrate('position_lots', row['payload']) for row in await paginate_all_rows(repositories.position_lots)]
    crypto_lots = [lot for lot in lots if lot.asset.asset_class == AssetClass.CRYPTO]
    if not crypto_lots:
        return True
    first_by_asset = {}
    for lot in crypto_lots:
        key = asset_identity_key(lot.asset)
        first_by_asset[key] = min(first_by_asset.get(key, lot.opened_at), lot.opened_at)
    subject = 'asset_fee_feed'
    if lease_lost is not None and lease_lost.is_set():
        return False
    try:
        # CFEE filtering is date-based; created_at is the precise accounting time.
        since = min(first_by_asset.values()).replace(hour=0, minute=0, second=0, microsecond=0)
        activities = await broker.get_activities(activity_type='CFEE', since=since)
        fees = [parse_asset_fee(activity.raw) for activity in activities]
        for fee in sorted(fees, key=lambda item: (item.occurred_at, item.activity_id)):
            subject = fee.activity_id
            key = asset_identity_key(fee.asset)
            if key not in first_by_asset or fee.occurred_at < first_by_asset[key]:
                continue  # outside this local accounting population
            if lease_lost is not None and lease_lost.is_set():
                logger.warning('asset_fee_lease_lost')
                return False
            applied = await apply_asset_fee(repositories, fee, now=now)
            if not applied:
                replay = ReconciliationRecord(str(uuid4()), 'asset_fee', fee.activity_id, ReconciliationOutcome.MATCHED,
                                              expected={'quantity_debit': fee.quantity}, actual={'ledger_record_id': f'asset_fee:{fee.activity_id}'},
                                              occurred_at=now)
                await repositories.reconciliation_records.create_once(replay.record_id, replay)
        record = ReconciliationRecord(str(uuid4()), 'asset_fee', 'asset_fee_feed', ReconciliationOutcome.MATCHED,
                                      expected={'receipt_verified': True}, actual={'activities_checked': len(fees)}, occurred_at=now)
        await repositories.reconciliation_records.create_once(record.record_id, record)
        return True
    except Exception as exc:  # noqa: BLE001 - preserve the reconciliation worker on provider/accounting failure
        record = ReconciliationRecord(str(uuid4()), 'asset_fee', subject, ReconciliationOutcome.DRIFT_DETECTED,
                                      expected={'receipt_verified': True}, actual={'error': str(exc)}, occurred_at=now)
        await repositories.reconciliation_records.create_once(record.record_id, record)
        logger.warning('asset_fee_reconciliation_failed', extra={'subject_id': subject, 'reason': str(exc)})
        if not isinstance(exc, AssetFeePending):
            await latch_financial_integrity_block(repositories, f'Asset fee integrity failure: {exc}', clock=lambda: now)
        return False
