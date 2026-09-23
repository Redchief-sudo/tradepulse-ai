"""Durable per-instrument accounting admission and gross-fill lifecycle.

All state transitions execute under SQLite's write transaction. Execution facts
remain immutable; an epoch is a separate statement about their finality.
"""
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import AssetClass, asset_identity_key
from tradepulse.persistence.codec import decode_payload, encode_payload


def _aware_utc(value, *, field_name):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f'{field_name} is naive and cannot represent a broker instant')
    return dt.astimezone(UTC)


def _generation_boundary(epoch):
    for key in ('generation_opened_at', 'verification_generation_opened_at', 'opened_at'):
        value = epoch.get(key)
        if value is None:
            continue
        return _aware_utc(value, field_name=key)
    raise ValueError('missing_generation_boundary')

STATES = frozenset({'submitted', 'filled_gross', 'fee_pending', 'reconciled_net', 'integrity_blocked'})


class AccountingEpochPending(ValueError):
    """An unresolved population prevents additional discretionary exposure."""


def epochs_for(connection, key):
    return [decode_payload(row['payload']) for row in connection.execute(
        "SELECT payload FROM accounting_epochs WHERE json_extract(payload,'$.canonical_asset_key')=? ORDER BY rowid", (key,))]


def save_epoch(connection, epoch, now):
    status = epoch['fee_accounting_status']
    if status not in STATES:
        raise ValueError('invalid accounting state')
    identifier = epoch['accounting_epoch_id']
    old = connection.execute('SELECT payload FROM accounting_epochs WHERE record_id=?', (identifier,)).fetchone()
    previous = decode_payload(old['payload']) if old else None
    prior_proof = previous.get('population_proof_id') if previous else None
    changed_checkpoint = previous is not None and (
        status != 'reconciled_net' or epoch.get('population_proof_id') != prior_proof)
    if previous and previous['fee_accounting_status'] == 'reconciled_net' and changed_checkpoint and prior_proof:
        superseded = list(epoch.get('superseded_checkpoint_ids', []))
        prior_checkpoint = previous.get('checkpoint_id')
        if prior_checkpoint and prior_checkpoint not in superseded:
            superseded.append(prior_checkpoint)
        epoch['superseded_checkpoint_ids'] = superseded
        receipt = {'record_id': 'epoch_proof_superseded:'+sha256(encode_payload({
            'epoch': identifier, 'proof': prior_proof, 'version': previous.get('checkpoint_version', 0)}).encode()).hexdigest(),
            'reconciliation_type': 'asset_fee', 'subject_id': identifier, 'outcome': 'drift_detected',
            'expected': {'active_checkpoint_id': prior_checkpoint, 'population_proof_id': prior_proof},
            'actual': {'superseded_checkpoint': previous, 'reason': epoch.get('reason', 'new_accounting_evidence')},
            'occurred_at': now.isoformat(), 'corrective_action': 'supersede_checkpoint_preserving_evidence'}
        connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (receipt['record_id'], encode_payload(receipt), now.isoformat()))
    if status == 'reconciled_net' and (previous is None or previous['fee_accounting_status'] != status
                                     or previous.get('population_proof_id') != epoch.get('population_proof_id')):
        epoch['checkpoint_version'] = (previous.get('checkpoint_version', 0) if previous else 0) + 1
        epoch['checkpoint_id'] = 'epoch_checkpoint:'+sha256(encode_payload({
            'epoch': identifier, 'version': epoch['checkpoint_version'], 'population': epoch['population_proof_id']}).encode()).hexdigest()
    payload = encode_payload(epoch)
    if old is not None and old['payload'] == payload:
        return
    connection.execute(
        'INSERT INTO accounting_epochs(record_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?) '
        'ON CONFLICT(record_id) DO UPDATE SET status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at',
        (identifier, status, payload, now.isoformat(), now.isoformat()))
    if status == 'reconciled_net':
        checkpoint = {'record_id': epoch['checkpoint_id'], 'reconciliation_type': 'asset_fee',
                      'subject_id': identifier, 'outcome': 'matched', 'expected': {'conservation': 'exact'},
                      'actual': epoch, 'occurred_at': now.isoformat(), 'corrective_action': 'versioned_accounting_checkpoint'}
        connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (checkpoint['record_id'], encode_payload(checkpoint), now.isoformat()))
    transition = {'record_id': 'epoch_transition:'+sha256(encode_payload({
        'previous': decode_payload(old['payload']) if old else None, 'next': epoch}).encode()).hexdigest(),
        'reconciliation_type': 'asset_fee', 'subject_id': identifier,
        'outcome': 'matched' if status == 'reconciled_net' else 'drift_detected',
        'expected': {'fee_accounting_status': 'reconciled_net'},
        'actual': epoch, 'occurred_at': now.isoformat(), 'corrective_action': 'accounting_epoch_transition'}
    connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                       (transition['record_id'], encode_payload(transition), now.isoformat()))


def new_epoch(key, identifier, now, starting_quantity, intent_ids):
    boundary = now.isoformat()
    return {'canonical_asset_key': key, 'accounting_epoch_id': identifier,
            'starting_broker_quantity': str(starting_quantity), 'ending_broker_quantity': None,
            'gross_filled_quantity': '0', 'asset_fee_quantity': '0', 'net_inventory_quantity': None,
            'cash_fee_amount': '0', 'fee_accounting_status': 'submitted',
            'broker_activity_cursor': None, 'broker_activity_window': None, 'fee_evidence_ids': [],
            'population_proof_id': None, 'population_hash': None, 'opened_at': boundary,
            'generation_opened_at': boundary,
            'generation_id': None,
            'verification_generation_id': None,
            'account_identity_digest': None,
            'opening_activity_cursor': None,
            'opening_activity_population_hash': None,
            'opening_positions': None,
            'opening_cash_equity': None,
            'reconciled_at': None, 'trade_intent_ids': list(intent_ids), 'fill_ids': [],
            'checkpoint_version': 0, 'checkpoint_id': None, 'superseded_checkpoint_ids': []}


async def reserve_epoch(repositories, intent, broker_quantity, *, protective, now):
    """Reserve before submission; a restart of the same intent is idempotent.

    Protective orders join the unresolved population, never a new entry epoch.
    The guard is scoped by canonical identity, not by the global session state.
    """
    if intent.asset.asset_class != AssetClass.CRYPTO:
        return None
    key = asset_identity_key(intent.asset)

    def reserve(connection):
        epochs = epochs_for(connection, key)
        own = next((e for e in epochs if intent.trade_intent_id in e['trade_intent_ids']), None)
        if own:
            return own['accounting_epoch_id']
        pending = [e for e in epochs if e['fee_accounting_status'] != 'reconciled_net']
        assigned = {i for e in epochs for i in e['trade_intent_ids']}
        legacy = [decode_payload(r['payload']) for r in connection.execute('SELECT payload FROM fills')]
        from tradepulse.persistence import hydrate
        unknown = any(asset_identity_key(hydrate('fills', f).asset) == key
                      and f['trade_intent_id'] not in assigned for f in legacy)
        if unknown and not protective:
            raise AccountingEpochPending('CRYPTO_ACCOUNTING_HISTORY_UNVERIFIED:'+key)
        if pending and not protective:
            raise AccountingEpochPending('CRYPTO_ACCOUNTING_EPOCH_PENDING:'+key)
        if pending:
            epoch = pending[-1]
            epoch['trade_intent_ids'].append(intent.trade_intent_id)
        else:
            epoch = new_epoch(key, 'crypto_epoch:'+intent.trade_intent_id, now, broker_quantity, [intent.trade_intent_id])
        save_epoch(connection, epoch, now)
        return epoch['accounting_epoch_id']
    return await repositories.accounting_epochs.database.run(reserve, write=True)


async def record_gross_fill(repositories, fill, *, now):
    """Insert a validated gross execution and its provisional epoch atomically.

    Settlement creation remains restart-repairable through its existing unique
    fill boundary. A crash cannot expose a new crypto fill as finalized net.
    """
    key = asset_identity_key(fill.asset)

    def write(connection):
        epochs = epochs_for(connection, key)
        epoch = next((e for e in epochs if fill.trade_intent_id in e['trade_intent_ids']), None)
        if epoch is None:
            # Recovery of pre-lifecycle history has no observed starting balance.
            # It remains pending until the complete history proves that balance.
            epoch = new_epoch(key, 'crypto_epoch:'+fill.trade_intent_id, fill.filled_at, None, [fill.trade_intent_id])
            epoch['starting_broker_quantity'] = None
        row = connection.execute('SELECT payload FROM fills WHERE record_id=?', (fill.fill_id,)).fetchone()
        if row and decode_payload(row['payload']) != decode_payload(encode_payload(fill)):
            raise ValueError('CRYPTO_GROSS_FILL_RECEIPT_CHANGED')
        connection.execute('INSERT OR IGNORE INTO fills(record_id,broker_fill_id,payload,created_at) VALUES(?,?,?,?)',
                           (fill.fill_id, fill.broker_fill_id, encode_payload(fill), now.isoformat()))
        if fill.fill_id in epoch['fill_ids']:
            return
        epoch['fill_ids'].append(fill.fill_id)
        epoch['gross_filled_quantity'] = str(Decimal(epoch['gross_filled_quantity'])+fill.quantity)
        epoch['fee_accounting_status'] = 'filled_gross'
        epoch['reconciled_at'] = None
        save_epoch(connection, epoch, now)
        epoch['fee_accounting_status'] = 'fee_pending'
        save_epoch(connection, epoch, now)
    await repositories.accounting_epochs.database.run(write, write=True)


async def pending_assets(repositories):
    def read(connection):
        return {decode_payload(row['payload'])['canonical_asset_key'] for row in connection.execute(
            "SELECT payload FROM accounting_epochs WHERE status != 'reconciled_net'")}
    return await repositories.accounting_epochs.database.run(read)


def finalize_population(connection, *, key, proof, population_id, fills, fees, cash_plans,
                        lots, quantity, activities, now, pagination=None, evidence_hash=None):
    """Commit inbox, cursor, and epoch proof alongside the accounting replay.

    A populated, conserved inventory is not sufficient to assume zero fees:
    buy populations require native receipts and sell populations require their
    proven cash-expense receipts. Incomplete populations remain provisional.
    """
    epochs = epochs_for(connection, key)
    assigned = {i for e in epochs for i in e['trade_intent_ids']}
    historical = sorted({f.trade_intent_id for f in fills}-assigned)
    if historical:
        prefix = 'crypto_epoch:history:' if fills[0].asset.asset_class == AssetClass.CRYPTO else 'accounting_epoch:history:'
        epoch = new_epoch(key, prefix+sha256(key.encode()).hexdigest(),
                          min(f.filled_at for f in fills), Decimal(0), historical)
        epoch['historical_population'] = True
        existing_history = next((e for e in epochs if e['accounting_epoch_id'] == epoch['accounting_epoch_id']), None)
        if existing_history is not None:
            existing_history.update(epoch)
        else:
            epochs.append(epoch)
    if pagination is not None:
        pagination = {k: pagination[k] for k in ('method', 'pages', 'activity_ids', 'complete', 'population_hash')}
    for raw in activities:
        old = connection.execute('SELECT payload FROM broker_activity_inbox WHERE record_id=?', (raw['id'],)).fetchone()
        if old and decode_payload(old['payload']) != raw:
            raise ValueError('BROKER_ACTIVITY_RECEIPT_CHANGED:'+raw['id'])
        connection.execute('INSERT OR IGNORE INTO broker_activity_inbox(record_id,payload,created_at) VALUES(?,?,?)',
                           (raw['id'], encode_payload(raw), now.isoformat()))
    cursor = activities[-1]['id'] if activities else None
    population_hash = population_id.split(':', 1)[1]
    for epoch in epochs:
        selected = [f for f in fills if f.trade_intent_id in epoch['trade_intent_ids']]
        if not selected:
            continue
        boundary = _generation_boundary(epoch)
        opened = boundary
        epoch_fees = [fee for fee in fees if fee.occurred_at.astimezone(UTC) >= boundary]
        # A later epoch does not consume fees already accounted in its starting
        # broker balance. Historical bootstrap covers the complete instrument.
        later = [_aware_utc(e['generation_opened_at'] if e.get('generation_opened_at') else e['opened_at'], field_name='opened_at')
                 for e in epochs if (e.get('generation_opened_at') or e.get('opened_at')) is not None and _aware_utc(
                     e.get('generation_opened_at') if e.get('generation_opened_at') else e['opened_at'], field_name='opened_at') > boundary]
        end = min(later) if later else None
        if end:
            epoch_fees = [fee for fee in epoch_fees if fee.occurred_at.astimezone(UTC) < end]
        selected_cash = [(raw, order, allocation) for raw, order, allocation in cash_plans
                         if set(order['trade_intent_ids']) & set(epoch['trade_intent_ids'])]
        buys = sum((f.quantity for f in selected if f.side.value == 'buy'), Decimal(0))
        sells = sum((f.quantity for f in selected if f.side.value == 'sell'), Decimal(0))
        native = sum((f.quantity for f in epoch_fees), Decimal(0))
        start = epoch['starting_broker_quantity']
        if start is None:
            # Exhaustive source history establishes a recovery epoch's starting
            # balance; never infer it from a stale local Holding.
            preceding = sum((f.quantity if f.side.value == 'buy' else -f.quantity
                             for f in fills if f.filled_at < opened), Decimal(0))
            preceding -= sum((fee.quantity for fee in fees if fee.occurred_at.astimezone(UTC) < boundary), Decimal(0))
            start = str(preceding)
            epoch['starting_broker_quantity'] = start
        terminal = True
        for intent_id in epoch['trade_intent_ids']:
            row = connection.execute('SELECT status FROM trade_intents WHERE record_id=?', (intent_id,)).fetchone()
            if row is None or row['status'] not in {'filled', 'canceled', 'expired', 'rejected'}:
                terminal = False
        if fills[0].asset.asset_class == AssetClass.CRYPTO:
            fee_complete = (not buys or bool(epoch_fees)) and (not sells or bool(selected_cash)) and terminal
        else:
            # The generation boundary is frozen at the verification opening, not the
            # first fill timestamp in an epoch. Historical receipts remain auditable,
            # but they cannot contaminate the active generation's fee status.
            generation_fees = [
                r for r in activities
                if r.get('activity_type') == 'FEE'
                and r.get('status') == 'executed'
                and r.get('currency') == 'USD'
                and (('created_at' in r and _aware_utc(r['created_at'], field_name='activity_created_at') >= boundary)
                     or ('transaction_time' in r and _aware_utc(r['transaction_time'], field_name='activity_transaction_time') >= boundary))
                and (not end or (('created_at' in r and _aware_utc(r['created_at'], field_name='activity_created_at') < end)
                                 or ('transaction_time' in r and _aware_utc(r['transaction_time'], field_name='activity_transaction_time') < end)))
            ]
            fee_complete = terminal and not generation_fees
        fee_complete = fee_complete and pagination is not None and pagination.get('complete') is True
        if end:
            # Closed epoch retains its established ending balance. The current
            # cumulative population revalidates source receipts independently.
            ending = epoch['ending_broker_quantity']
        else:
            ending = str(quantity)
        conserved = start is not None and ending is not None and Decimal(start)+buys-sells-native == Decimal(ending)
        status = 'reconciled_net' if conserved and fee_complete else 'fee_pending'
        epoch.pop('reason', None)
        if fills[0].asset.asset_class != AssetClass.CRYPTO and not fee_complete:
            epoch['reason'] = 'unallocated_account_fee_or_nonterminal_order'
        epoch.update(gross_filled_quantity=str(buys+sells), asset_fee_quantity=str(native),
                     net_inventory_quantity=ending if status == 'reconciled_net' else None,
                     ending_broker_quantity=ending,
                     cash_fee_amount=str(sum((amount for _, _, allocations in selected_cash for identifier, amount in allocations.items()
                         if any(identifier == lot.lot_id+':'+f.fill_id for lot in lots for f in selected)), Decimal(0))),
                     fee_accounting_status=status, broker_activity_cursor=cursor,
                     broker_activity_window={'from': epoch['opened_at'], 'through_activity_id': cursor},
                     fee_evidence_ids=sorted([f.activity_id for f in epoch_fees]+[r['id'] for r, _, _ in selected_cash]),
                     population_proof_id=population_id, population_hash=population_hash,
                     fill_ids=sorted(f.fill_id for f in selected),
                     included_activity_ids=sorted([f.broker_fill_id for f in selected]+[f.activity_id for f in epoch_fees]
                                                 +[r['id'] for r, _, _ in selected_cash]),
                     pagination_proof=pagination, evidence_file_sha256=evidence_hash,
                     conservation={'starting_broker_quantity': start, 'buys': str(buys), 'sells': str(sells),
                                   'asset_fees': str(native), 'ending_broker_quantity': ending,
                                   'holding_quantity': str(quantity),
                                   'remaining_lot_quantity': str(sum((l.signed_quantity for l in lots), Decimal(0)))})
        epoch['excluded_activity_ids'] = sorted({r['id'] for r in activities}-set(epoch['included_activity_ids']))
        if status == 'reconciled_net' and not epoch['reconciled_at']:
            epoch['reconciled_at'] = now.isoformat()
        elif status != 'reconciled_net':
            epoch['reconciled_at'] = None
        save_epoch(connection, epoch, now)
    # Per-asset cursor: a different instrument cannot advance a failed replay.
    payload = encode_payload({'canonical_asset_key': key, 'last_complete_activity_id': cursor,
                              'population_proof_id': population_id, 'population_hash': population_hash,
                              'pagination_proof': pagination})
    old = connection.execute('SELECT payload FROM broker_activity_cursors WHERE record_id=?', (key,)).fetchone()
    if old is None or old['payload'] != payload:
        connection.execute('INSERT INTO broker_activity_cursors(record_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?) '
                           'ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                           (key, 'complete', payload, now.isoformat(), now.isoformat()))


async def block_asset(repositories, asset, reason, *, now, pending=False):
    key = asset_identity_key(asset)
    def write(connection):
        epochs = epochs_for(connection, key)
        epoch = epochs[-1] if epochs else new_epoch(key, 'crypto_epoch:history:'+sha256(key.encode()).hexdigest(), now, None, [])
        if not epochs:
            epoch['starting_broker_quantity'] = None
        epoch['fee_accounting_status'] = 'fee_pending' if pending else 'integrity_blocked'
        epoch['reason'] = reason
        epoch['reconciled_at'] = None
        save_epoch(connection, epoch, now)
    await repositories.accounting_epochs.database.run(write, write=True)


def verify_checkpoint(epoch, records, fills, intents):
    """Validate persisted checkpoint provenance independently of mutable state."""
    from tradepulse.persistence import hydrate

    from .activity_cursor import validate_pagination
    from .fee_population import validate_fee_population
    if epoch['fee_accounting_status'] != 'reconciled_net':
        raise ValueError('CHECKPOINT_NOT_RECONCILED')
    checkpoint = records[epoch['checkpoint_id']]
    if checkpoint['outcome'] != 'matched' or encode_payload(checkpoint['actual']) != encode_payload(epoch):
        raise ValueError('CHECKPOINT_RECEIPT_MISMATCH')
    identifier = 'epoch_checkpoint:'+sha256(encode_payload({
        'epoch': epoch['accounting_epoch_id'], 'version': epoch['checkpoint_version'],
        'population': epoch['population_proof_id']}).encode()).hexdigest()
    if identifier != epoch['checkpoint_id'] or identifier in epoch.get('superseded_checkpoint_ids', []):
        raise ValueError('CHECKPOINT_VERSION_INVALID')
    proof = records[epoch['population_proof_id']]['actual']
    digest = sha256(encode_payload(proof).encode()).hexdigest()
    if epoch['population_hash'] != digest or epoch['population_proof_id'] != 'asset_fee_population:'+digest:
        raise ValueError('CHECKPOINT_POPULATION_HASH_MISMATCH')
    activities = {raw['id']: raw for raw in proof['activities']}
    pagination = epoch['pagination_proof']
    if set(pagination['activity_ids']) != activities.keys():
        raise ValueError('CHECKPOINT_PAGINATION_POPULATION_MISMATCH')
    validate_pagination([activities[i] for i in pagination['activity_ids']], pagination)
    source_fills = [hydrate('fills', f) for f in fills if f['broker_fill_id'] in activities]
    asset = next(f.asset for f in source_fills if asset_identity_key(f.asset) == epoch['canonical_asset_key'])
    _, verified = validate_fee_population(asset, proof['activities'], source_fills,
        [hydrate('trade_intents', i) for i in intents], Decimal(proof['broker_quantity']))
    if encode_payload(verified) != encode_payload(proof):
        raise ValueError('CHECKPOINT_POPULATION_CHANGED')
    c = epoch['conservation']
    if (Decimal(c['starting_broker_quantity'])+Decimal(c['buys'])-Decimal(c['sells'])-Decimal(c['asset_fees'])
            != Decimal(c['ending_broker_quantity']) or epoch['net_inventory_quantity'] != c['ending_broker_quantity']):
        raise ValueError('CHECKPOINT_QUANTITY_NOT_CONSERVED')
