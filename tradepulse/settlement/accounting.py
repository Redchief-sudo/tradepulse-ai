"""Canonical fill cash and realized-PnL projections, version 1.

Cash is a signed USD movement, including the immutable fill's explicit fees.
PnL is gross price PnL per closed-lot attribution, with explicit fill fees
recognized separately as expenses at execution time. Broker cash remains an
external reconciliation observation, not a substitute for these journal rows.
All synchronous helpers run only inside AsyncSQLiteDatabase.run transactions.
"""
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import CashLedgerEntry, PnlRecord, Side, asset_identity_key, contract_multiplier_of
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.persistence.repositories import _check_integrity_hold


class ProjectionEvidenceError(ValueError):
    """Retryable missing evidence; the settlement batch owns failure handling."""


def _get(connection, table, identifier):
    row = connection.execute(f'SELECT payload FROM {table} WHERE record_id=?', (identifier,)).fetchone()
    return decode_payload(row['payload']) if row else None


def source_fill(connection, event):
    raw = _get(connection, 'fills', event.fill_id)
    if raw is None:
        raise ProjectionEvidenceError('ACCOUNTING_FILL_MISSING:' + event.fill_id)
    fill = hydrate('fills', raw)
    if (any(getattr(fill, field) != getattr(event, field) for field in
            ('trade_intent_id', 'side', 'execution_mode', 'quantity', 'price', 'fees'))
            or asset_identity_key(fill.asset) != asset_identity_key(event.asset)
            or fill.filled_at != event.occurred_at
            or (event.broker_order_id is not None and fill.order_id != event.broker_order_id)
            or fill.broker_fill_id != event.broker_fill_id):
        raise ProjectionEvidenceError('ACCOUNTING_FILL_SETTLEMENT_MISMATCH:' + event.fill_id)
    return fill


def cash_evidence(connection, event):
    fill = source_fill(connection, event)
    identifier = 'fill:cash:' + fill.fill_id
    notional = fill.quantity * fill.price * contract_multiplier_of(fill.asset)
    return CashLedgerEntry(identifier, identifier,
                           (notional if fill.side == Side.SELL else -notional) - fill.fees,
                           'USD', fill.filled_at, 'immutable fill cash; accounting-v1')


def pnl_evidence(connection, event):
    fill = source_fill(connection, event)
    key = asset_identity_key(fill.asset)
    records = []
    allocated = Decimal(0)
    realized = Decimal(0)
    expected_attrs = set()
    for row in connection.execute('SELECT payload FROM position_lots'):
        lot = hydrate('position_lots', decode_payload(row['payload']))
        if asset_identity_key(lot.asset) != key:
            continue
        if lot.originating_fill_id == fill.fill_id:
            if (lot.acquisition_price != fill.price or lot.opened_at != fill.filled_at
                    or lot.position_side != ('long' if fill.side == Side.BUY else 'short')
                    or lot.remaining_quantity + sum(lot.closures.values(), Decimal(0))
                       + sum(lot.asset_fee_quantities.values(), Decimal(0)) != lot.opened_quantity):
                raise ProjectionEvidenceError('ACCOUNTING_OPENING_LOT_MISMATCH:' + lot.lot_id)
            allocated += lot.opened_quantity
        quantity = lot.closures.get(fill.fill_id)
        if quantity is None:
            continue
        opening_raw = _get(connection, 'fills', lot.originating_fill_id)
        if opening_raw is None:
            raise ProjectionEvidenceError('ACCOUNTING_OPENING_FILL_MISSING:' + lot.lot_id)
        opening = hydrate('fills', opening_raw)
        if (asset_identity_key(opening.asset) != key or opening.price != lot.acquisition_price
                or opening.filled_at != lot.opened_at or opening.side == fill.side
                or lot.opened_quantity > opening.quantity or quantity <= 0
                or lot.remaining_quantity + sum(lot.closures.values(), Decimal(0))
                   + sum(lot.asset_fee_quantities.values(), Decimal(0)) != lot.opened_quantity):
            raise ProjectionEvidenceError('ACCOUNTING_LOT_SOURCE_MISMATCH:' + lot.lot_id)
        identifier = lot.lot_id + ':' + fill.fill_id
        raw = _get(connection, 'trade_attributions', identifier)
        if raw is None:
            raise ProjectionEvidenceError('ACCOUNTING_ATTRIBUTION_MISSING:' + identifier)
        attr = hydrate('trade_attributions', raw)
        gross = (fill.price - opening.price) * quantity * contract_multiplier_of(fill.asset)
        if lot.position_side == 'short':
            gross = -gross
        if (attr.lot_id != lot.lot_id or attr.closing_fill_id != fill.fill_id
                or attr.quantity != quantity or attr.realized_pnl != gross
                or attr.entry_price != opening.price or attr.exit_price != fill.price
                or attr.entry_at != opening.filled_at or attr.exit_at != fill.filled_at
                or attr.opening_trade_intent_id != opening.trade_intent_id
                or attr.closing_trade_intent_id != fill.trade_intent_id
                or asset_identity_key(attr.asset) != key):
            raise ProjectionEvidenceError('ACCOUNTING_ATTRIBUTION_MISMATCH:' + identifier)
        expected_attrs.add(identifier)
        allocated += quantity
        realized += gross
        records.append(PnlRecord('fill:pnl:' + identifier, fill.asset, gross, Decimal(0), fill.filled_at))
    actual_attrs = {r['record_id'] for r in connection.execute(
        "SELECT record_id FROM trade_attributions WHERE json_extract(payload,'$.closing_fill_id')=?", (fill.fill_id,))}
    if allocated != fill.quantity or actual_attrs != expected_attrs or event.realized_pnl != realized:
        raise ProjectionEvidenceError('ACCOUNTING_CLOSURE_CONSERVATION_FAILED:' + fill.fill_id)
    if fill.fees:
        records.append(PnlRecord('fill:fee:' + fill.fill_id, fill.asset, -fill.fees, Decimal(0), fill.filled_at))
    return records


def _persist(connection, table, identifier, item, *, write):
    expected = decode_payload(encode_payload(item))
    existing = _get(connection, table, identifier)
    if existing is not None:
        if existing != expected:
            raise ProjectionEvidenceError('ACCOUNTING_DURABLE_ROW_CONFLICT:' + identifier)
        return False
    if not write:
        raise ProjectionEvidenceError('ACCOUNTING_DURABLE_ROW_MISSING:' + identifier)
    columns = 'record_id,payload,created_at'
    values = [identifier, encode_payload(item), (item.occurred_at if table == 'cash_ledger' else item.as_of).isoformat()]
    if table == 'cash_ledger':
        columns += ',idempotency_key'
        values.append(item.idempotency_key)
    connection.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})', values)
    return True


def project(connection, event, *, cash=False, trade=False, write=False):
    """Validate expected rows, optionally persist; never trust checkpoint flags."""
    changed = 0
    if write:
        _check_integrity_hold(connection, 'integrity_holds', event.broker_order_id)
    if cash:
        item = cash_evidence(connection, event)
        changed += _persist(connection, 'cash_ledger', item.entry_id, item, write=write)
    if trade:
        for item in pnl_evidence(connection, event):
            changed += _persist(connection, 'pnl_records', item.record_id, item, write=write)
        if _get(connection, 'trade_intents', event.trade_intent_id) is None:
            raise ProjectionEvidenceError('ACCOUNTING_TRADE_INTENT_MISSING:' + event.trade_intent_id)
    return changed


async def project_accounting(repositories, event, *, cash=False, trade=False):
    return await repositories.settlements.database.run(
        lambda connection: project(connection, event, cash=cash, trade=trade, write=True), write=True)


async def replay_accounting(repositories):
    """Repair only proven completed projections, atomically, without flag edits.

    A missing/conflicting source aborts the entire replay. Immutable source
    hashes and algorithm version are retained in an idempotent correction receipt.
    """
    def replay(connection):
        changed = 0
        sources = {}
        for row in connection.execute("SELECT payload FROM settlements WHERE status='completed' ORDER BY record_id"):
            original = decode_payload(row['payload'])
            event = hydrate('settlements', original)
            changed += project(connection, event, cash=True, trade=True, write=True)
            # Explicit versioned migration for settlements predating the
            # attribution stage. Validate the complete attribution population
            # above before recording the newly introduced checkpoint field.
            if 'attribution_projected' not in original:
                migrated = replace(event, attribution_projected=True)
                successor = decode_payload(encode_payload(migrated))
                migration_id = 'accounting_stage_migration:v1:' + sha256(encode_payload({
                    'before': original, 'after': successor}).encode()).hexdigest()
                receipt = {'record_id': migration_id, 'reconciliation_type': 'accounting_migration',
                    'subject_id': event.settlement_event_id, 'outcome': 'corrected',
                    'expected': {'prior_contract': 'pre_attribution_stage'},
                    'actual': {'algorithm_version': 1, 'before': original, 'after': successor,
                               'evidence': 'canonical fill, lot allocation, attribution, cash and PnL verified in this transaction'},
                    'occurred_at': event.occurred_at.isoformat(),
                    'corrective_action': 'verified attribution-stage contract migration'}
                connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                    (migration_id, encode_payload(receipt), receipt['occurred_at']))
                connection.execute('UPDATE settlements SET payload=? WHERE record_id=?',
                                   (encode_payload(migrated), event.settlement_event_id))
        for table in ('fills', 'position_lots', 'trade_attributions', 'settlements'):
            sources[table] = sha256(encode_payload([decode_payload(r['payload']) for r in connection.execute(
                f'SELECT payload FROM {table} ORDER BY record_id')]).encode()).hexdigest()
        if changed:
            identifier = 'accounting_replay:v1:' + sha256(encode_payload(sources).encode()).hexdigest()
            payload = {'record_id': identifier, 'reconciliation_type': 'accounting_projection',
                       'subject_id': 'completed_settlements', 'outcome': 'corrected',
                       'expected': {'durable_cash_and_pnl': True},
                       'actual': {'algorithm_version': 1, 'source_sha256': sources, 'inserted_rows': changed},
                       'occurred_at': event.occurred_at.isoformat(),
                       'corrective_action': 'rebuild missing canonical cash and PnL from immutable fills and verified lot attributions'}
            connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                               (identifier, encode_payload(payload), payload['occurred_at']))
        return changed
    return await repositories.settlements.database.run(replay, write=True)


async def accounting_issues(repositories):
    """One consistent read snapshot; flags alone cannot pass reconciliation."""
    def check(connection):
        connection.execute('BEGIN')
        issues = {}
        for row in connection.execute('SELECT payload FROM settlements'):
            event = hydrate('settlements', decode_payload(row['payload']))
            try:
                if event.status.value != 'completed' or not all(getattr(event, flag) for flag in (
                        'lot_projected', 'attribution_projected', 'holding_projected',
                        'cash_projected', 'trade_projected', 'integrity_verified')):
                    raise ProjectionEvidenceError('ACCOUNTING_SETTLEMENT_INCOMPLETE')
                project(connection, event, cash=True, trade=True)
            except (ValueError, ArithmeticError, KeyError) as exc:
                issues[event.fill_id] = str(exc)
        for row in connection.execute('SELECT record_id FROM fills WHERE record_id NOT IN (SELECT fill_id FROM settlements)'):
            issues[row['record_id']] = 'ACCOUNTING_SETTLEMENT_MISSING'
        return issues
    return await repositories.settlements.database.run(check)
