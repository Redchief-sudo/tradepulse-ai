"""Durable per-instrument accounting admission and gross-fill lifecycle.

All state transitions execute under SQLite's write transaction. Execution facts
remain immutable; an epoch is a separate statement about their finality.
"""
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import AssetClass, asset_identity_key
from tradepulse.persistence.codec import decode_payload, encode_payload


from tradepulse.time import aware_utc


def _generation_boundary(epoch):
    return aware_utc(epoch.get('generation_opened_at'), field_name='generation_opened_at')


def _opening(connection):
    from tradepulse.verification.opening import load_bound_opening_checkpoint
    return load_bound_opening_checkpoint(connection) if connection is not None else None

def _verify_opening_reference(epoch, checkpoint):
    fields = {'verification_generation_id': 'verification_generation_id',
              'generation_opening_checkpoint_id': 'checkpoint_id', 'generation_opened_at': 'opened_at',
              'opening_activity_cursor': 'opening_activity_cursor',
              'opening_activity_population_hash': 'opening_activity_population_hash',
              'account_identity_digest': 'account_identity_digest'}
    if any(epoch.get(field) != checkpoint[key] for field, key in fields.items()):
        raise ValueError('EPOCH_GENERATION_OPENING_REFERENCE_INVALID')
    _generation_boundary(epoch)


STATES = frozenset({'submitted', 'filled_gross', 'fee_pending', 'reconciled_net', 'integrity_blocked'})


class AccountingEpochPending(ValueError):
    """An unresolved population prevents additional discretionary exposure."""


def epochs_for(connection, key):
    return [decode_payload(row['payload']) for row in connection.execute(
        "SELECT payload FROM accounting_epochs WHERE json_extract(payload,'$.canonical_asset_key')=? ORDER BY rowid", (key,))]


def save_epoch(connection, epoch, now):
    now = aware_utc(now, field_name='epoch_updated_at')
    checkpoint = _opening(connection)
    if checkpoint:
        _verify_opening_reference(epoch, checkpoint)
    status = epoch['fee_accounting_status']
    if status not in STATES:
        raise ValueError('invalid accounting state')
    identifier = epoch['accounting_epoch_id']
    old = connection.execute('SELECT payload FROM accounting_epochs WHERE record_id=?', (identifier,)).fetchone()
    previous = decode_payload(old['payload']) if old else None
    prior_proof = previous.get('population_proof_id') if previous else None
    prior_receipt = None
    if previous and previous.get('checkpoint_id'):
        row = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?',
                                 (previous['checkpoint_id'],)).fetchone()
        prior_receipt = decode_payload(row['payload']) if row else None
    receipt_matches = bool(prior_receipt and prior_receipt.get('outcome') == 'matched'
                           and encode_payload(prior_receipt.get('actual')) == encode_payload(previous))
    # A checkpoint binds the entire epoch, including its original pagination
    # observation. Refreshing the same population can change that evidence;
    # never update the epoch while retaining an incompatible immutable receipt.
    # Reconciled successors are produced only by finalize_population after the
    # caller has verified the complete population and accounting replay.
    changed_checkpoint = previous is not None and (
        status != 'reconciled_net' or encode_payload(epoch) != encode_payload(previous)
        or not receipt_matches)
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
            'actual': {'superseded_checkpoint': prior_receipt['actual'] if prior_receipt else previous,
                       'reason': epoch.get('reason', 'new_accounting_evidence')},
            'occurred_at': now.isoformat(), 'corrective_action': 'supersede_checkpoint_preserving_evidence'}
        if not receipt_matches:
            receipt['actual']['observed_epoch'] = previous
            receipt['actual']['prior_checkpoint_receipt_mismatch'] = True
        connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (receipt['record_id'], encode_payload(receipt), now.isoformat()))
    if status == 'reconciled_net' and (previous is None or previous['fee_accounting_status'] != status
                                     or changed_checkpoint):
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


def new_epoch(key, identifier, now, starting_quantity, intent_ids, *, connection=None):
    opened = aware_utc(now, field_name='epoch_opened_at')
    checkpoint = _opening(connection)
    epoch = {'canonical_asset_key': key, 'accounting_epoch_id': identifier,
            'starting_broker_quantity': str(starting_quantity) if starting_quantity is not None else None,
            'ending_broker_quantity': None,
            'gross_filled_quantity': '0', 'asset_fee_quantity': '0', 'net_inventory_quantity': None,
            'cash_fee_amount': '0', 'fee_accounting_status': 'submitted',
            'broker_activity_cursor': None, 'broker_activity_window': None, 'fee_evidence_ids': [],
            'population_proof_id': None, 'population_hash': None, 'opened_at': opened.isoformat(),
            'reconciled_at': None, 'trade_intent_ids': list(intent_ids), 'fill_ids': [],
            'checkpoint_version': 0, 'checkpoint_id': None, 'superseded_checkpoint_ids': []}
    if checkpoint is not None:
        epoch.update(verification_generation_id=checkpoint['verification_generation_id'],
                     generation_opening_checkpoint_id=checkpoint['checkpoint_id'],
                     generation_opened_at=checkpoint['opened_at'],
                     opening_activity_cursor=checkpoint['opening_activity_cursor'],
                     opening_activity_population_hash=checkpoint['opening_activity_population_hash'],
                     account_identity_digest=checkpoint['account_identity_digest'])
    return epoch


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
            from .membership import opening_quantities
            owned_quantity = broker_quantity - opening_quantities(_opening(connection)).get(key, Decimal(0))
            epoch = new_epoch(key, 'crypto_epoch:'+intent.trade_intent_id, now, owned_quantity, [intent.trade_intent_id], connection=connection)
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
            epoch = new_epoch(key, 'crypto_epoch:'+fill.trade_intent_id, fill.filled_at, None, [fill.trade_intent_id], connection=connection)
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
                        lots, quantity, activities, now, pagination=None, evidence_hash=None, membership=None):
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
                          min(f.filled_at for f in fills), Decimal(0), historical, connection=connection)
        epoch['historical_population'] = True
        existing_history = next((e for e in epochs if e['accounting_epoch_id'] == epoch['accounting_epoch_id']), None)
        if existing_history is not None:
            existing_history['trade_intent_ids'] = sorted(set(existing_history['trade_intent_ids']) | set(historical))
            existing_history['reconciled_at'] = None
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
        # A fee follows the inventory lot it actually debits. Epoch-local time
        # windows cannot assign a receipt or define a verification generation.
        selected_ids = {f.fill_id for f in selected}
        own_lots = [lot for lot in lots if lot.originating_fill_id in selected_ids]
        epoch_fees = [fee for fee in fees if any(
            fee.activity_id in lot.asset_fee_quantities for lot in own_lots)]
        later = [e for e in epochs if aware_utc(e['opened_at']) > aware_utc(epoch['opened_at'])]
        end = bool(later)
        selected_cash = [(raw, order, allocation) for raw, order, allocation in cash_plans
                         if set(order['trade_intent_ids']) & set(epoch['trade_intent_ids'])]
        buys = sum((f.quantity for f in selected if f.side.value == 'buy'), Decimal(0))
        sells = sum((f.quantity for f in selected if f.side.value == 'sell'), Decimal(0))
        native = sum((f.quantity for f in epoch_fees), Decimal(0))
        start = epoch['starting_broker_quantity']
        if start is None:
            # Recovery on a clean generation starts with zero owned inventory;
            # existing broker inventory is separately conserved by its checkpoint.
            preceding = sum((f.quantity if f.side.value == 'buy' else -f.quantity
                             for f in fills if aware_utc(f.filled_at) < aware_utc(epoch['opened_at'])), Decimal(0))
            preceding -= sum((lot.asset_fee_quantities.get(fee.activity_id, Decimal(0))
                              for fee in fees for lot in lots
                              if lot.originating_fill_id not in selected_ids), Decimal(0))
            start = str(preceding)
            epoch['starting_broker_quantity'] = start
        terminal = True
        for intent_id in epoch['trade_intent_ids']:
            row = connection.execute('SELECT status FROM trade_intents WHERE record_id=?', (intent_id,)).fetchone()
            if row is None or row['status'] not in {'filled', 'canceled', 'expired', 'rejected'}:
                terminal = False
        from .generation_fees import cash_fee_receipt, generation_fee_recorded
        from .membership import ELIGIBLE_MEMBERSHIPS
        eligible = [r for r in activities if membership is None or
                    membership['classifications'][r['id']] in ELIGIBLE_MEMBERSHIPS]
        cash_receipts = [(r, fee) for r in eligible if (fee := cash_fee_receipt(r)) is not None]
        account_fees = [r for r, fee in cash_receipts if not fee['relationship']]
        own_orders = {f.order_id for f in selected}
        own_fills = {f.broker_fill_id for f in selected}
        symbol = selected[0].asset.symbol.replace('/', '')
        linked_cash = [r for r, fee in cash_receipts if (
            fee['relationship'].get('order_id') in own_orders
            or fee['relationship'].get('fill_id') in own_fills
            or (not any(k in fee['relationship'] for k in ('order_id', 'fill_id'))
                and str(fee['relationship'].get('symbol') or '').replace('/', '') == symbol))]
        account_fees_conserved = all(generation_fee_recorded(connection, r['id']) for r, _ in cash_receipts)
        raw_by_id = {r['id']: r for r in eligible}
        def reported_fill_fee(fill):
            raw = raw_by_id[fill.broker_fill_id]
            value = next((raw[k] for k in ('fee', 'fees', 'commission') if raw.get(k) is not None), None)
            return (value is not None and fill.fee_source == 'broker_activity' and fill.fee_currency == 'USD'
                    and (raw.get('fee_currency') or raw.get('currency')) == 'USD'
                    and not isinstance(value, (float, bool)) and Decimal(str(value)) == fill.fees)
        buy_receipts = all(reported_fill_fee(f) for f in selected if f.side.value == 'buy')
        sell_receipts = all(reported_fill_fee(f) for f in selected if f.side.value == 'sell')
        cash_evidenced = bool(account_fees or linked_cash)
        if fills[0].asset.asset_class == AssetClass.CRYPTO:
            # A durably conserved account expense closes the generation fee
            # obligation without fabricating an allocation to this asset.
            fee_complete = (not buys or bool(epoch_fees) or cash_evidenced or buy_receipts) and (
                not sells or bool(selected_cash) or cash_evidenced or sell_receipts)
        else:
            fee_complete = not sells or cash_evidenced or sell_receipts
        fee_complete = (fee_complete and terminal and account_fees_conserved
                        and pagination is not None and pagination.get('complete') is True)
        ending = str(Decimal(start)+buys-sells-native) if end and start is not None else str(quantity)
        conserved = start is not None and ending is not None and Decimal(start)+buys-sells-native == Decimal(ending)
        status = 'reconciled_net' if conserved and fee_complete else 'fee_pending'
        epoch.pop('reason', None)
        if fills[0].asset.asset_class != AssetClass.CRYPTO and not fee_complete:
            epoch['reason'] = 'missing_fee_receipt_or_nonterminal_order'
        epoch.update(gross_filled_quantity=str(buys+sells), asset_fee_quantity=str(native),
                     net_inventory_quantity=ending if status == 'reconciled_net' else None,
                     ending_broker_quantity=ending,
                     cash_fee_amount=str(sum((amount for _, _, allocations in selected_cash for identifier, amount in allocations.items()
                         if any(identifier == lot.lot_id+':'+f.fill_id for lot in lots for f in selected)), Decimal(0))),
                     fee_accounting_status=status, broker_activity_cursor=cursor,
                     broker_activity_window={'from': epoch['opened_at'], 'through_activity_id': cursor},
                     fee_evidence_ids=sorted([f.activity_id for f in epoch_fees]+[r['id'] for r, _, _ in selected_cash]),
                     generation_fee_evidence_ids=sorted({r['id'] for r in account_fees + linked_cash}),
                     fee_evidence_state=('receipt_backed' if cash_evidenced or epoch_fees or selected_cash
                         or (buy_receipts and sell_receipts) else 'complete_population_no_fee_observed'),
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
        epoch = epochs[-1] if epochs else new_epoch(key, 'crypto_epoch:history:'+sha256(key.encode()).hexdigest(), now, None, [], connection=connection)
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
    membership = proof.get('generation_membership')
    if membership:
        _verify_opening_reference(epoch, membership['checkpoint'])
        from .membership import verify_membership_record
        classifications = verify_membership_record(membership['checkpoint'], records[membership['record_id']])
        if classifications != membership['classifications']:
            raise ValueError('CHECKPOINT_GENERATION_MEMBERSHIP_CHANGED')
    elif epoch.get('verification_generation_id'):
        raise ValueError('CHECKPOINT_GENERATION_MEMBERSHIP_MISSING')
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
        [hydrate('trade_intents', i) for i in intents], Decimal(proof['broker_quantity']),
        membership=proof.get('generation_membership'))
    if encode_payload(verified) != encode_payload(proof):
        raise ValueError('CHECKPOINT_POPULATION_CHANGED')
    c = epoch['conservation']
    if (Decimal(c['starting_broker_quantity'])+Decimal(c['buys'])-Decimal(c['sells'])-Decimal(c['asset_fees'])
            != Decimal(c['ending_broker_quantity']) or epoch['net_inventory_quantity'] != c['ending_broker_quantity']):
        raise ValueError('CHECKPOINT_QUANTITY_NOT_CONSERVED')


def checkpoint_issues(connection):
    """Independently verify every current epoch in the caller's read snapshot.

    Include closed instruments and every local fill; an empty epoch table must
    not make an existing execution population appear verified. No state, proof,
    or reconciliation receipt is created or repaired by this function.
    """
    from collections import Counter, defaultdict
    from tradepulse.persistence import hydrate

    issues = {}
    tables = {}
    for table in ('fills', 'trade_intents', 'position_lots', 'holdings', 'reconciliation_records'):
        tables[table] = {}
        for row in connection.execute(f'SELECT record_id,payload FROM {table} ORDER BY record_id'):
            try:
                tables[table][row['record_id']] = decode_payload(row['payload'])
            except (ValueError, TypeError) as exc:
                issues[table + ':' + row['record_id']] = str(exc)
    fills = tables['fills']
    intents = tables['trade_intents']
    records = tables['reconciliation_records']
    fills_by_asset = defaultdict(set)
    lots_by_asset = defaultdict(Decimal)
    holdings_by_asset = defaultdict(Decimal)
    epochs_by_asset = defaultdict(list)
    covered = Counter()
    for table, destination in (('fills', fills_by_asset), ('position_lots', lots_by_asset),
                               ('holdings', holdings_by_asset)):
        for identifier, raw in tables[table].items():
            try:
                item = hydrate(table, raw)
                key = asset_identity_key(item.asset)
                if table == 'fills':
                    if item.fill_id != identifier:
                        raise ValueError('CHECKPOINT_FILL_IDENTITY_MISMATCH')
                    destination[key].add(identifier)
                elif table == 'position_lots':
                    destination[key] += item.signed_quantity
                else:
                    if key != identifier:
                        raise ValueError('CHECKPOINT_HOLDING_IDENTITY_MISMATCH')
                    destination[key] += item.quantity
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                issues[table + ':' + identifier] = str(exc)
    for row in connection.execute('SELECT record_id,status,payload FROM accounting_epochs ORDER BY record_id'):
        identifier = row['record_id']
        try:
            epoch = decode_payload(row['payload'])
            key = epoch['canonical_asset_key']
            epochs_by_asset[key].append(epoch)
            if identifier != epoch['accounting_epoch_id']:
                raise ValueError('CHECKPOINT_EPOCH_IDENTITY_MISMATCH')
            if row['status'] != epoch['fee_accounting_status']:
                raise ValueError('CHECKPOINT_EPOCH_STATUS_MISMATCH')
            expected = {fid for fid in fills_by_asset[key]
                        if fills[fid]['trade_intent_id'] in epoch['trade_intent_ids']}
            declared = epoch['fill_ids']
            if not expected or len(declared) != len(set(declared)) or set(declared) != expected:
                raise ValueError('CHECKPOINT_EPOCH_FILL_POPULATION_MISMATCH')
            covered.update(declared)
            verify_checkpoint(epoch, records, list(fills.values()), list(intents.values()))
        except (ValueError, KeyError, TypeError, ArithmeticError, StopIteration) as exc:
            issues[identifier] = str(exc)
    for identifier in sorted(fills):
        if covered[identifier] != 1:
            issues['fill:' + identifier] = ('CHECKPOINT_FILL_MEMBERSHIP_MISSING' if not covered[identifier]
                                            else 'CHECKPOINT_FILL_MEMBERSHIP_DUPLICATED')
    for key in sorted(set(fills_by_asset) | set(lots_by_asset) | set(holdings_by_asset) | set(epochs_by_asset)):
        epochs = epochs_by_asset[key]
        if not epochs:
            issues['asset:' + key] = 'CHECKPOINT_EPOCH_MISSING'
            continue
        try:
            latest = max(epochs, key=lambda epoch: aware_utc(epoch['opened_at'], field_name='epoch_opened_at'))
            quantity = Decimal(latest['ending_broker_quantity'])
            if (not quantity.is_finite() or quantity != lots_by_asset[key]
                    or quantity != holdings_by_asset[key]):
                raise ValueError('CHECKPOINT_CURRENT_QUANTITY_MISMATCH')
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            issues['asset:' + key] = str(exc)
    return issues


_UNLOCK_TABLES = (
    'trading_sessions', 'trade_intents', 'orders', 'fills', 'settlements',
    'position_lots', 'holdings', 'cash_ledger', 'pnl_records', 'trade_attributions',
    'accounting_epochs', 'broker_activity_inbox', 'broker_activity_cursors',
    'reconciliation_records', 'integrity_holds', 'verification_identity',
)


def financial_evidence_digest(connection):
    """Bind the reset to financial rows, session, and generation identity."""
    parts = []
    for table in _UNLOCK_TABLES:
        key = 'singleton' if table == 'verification_identity' else 'record_id'
        parts.append((table, [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY {key}')]))
    return sha256(encode_payload(parts).encode()).hexdigest()


def unlock_proof(connection):
    """Independent checkpoint verification plus a digest of financial evidence."""
    if not connection.in_transaction:
        connection.execute('BEGIN')
    issues = checkpoint_issues(connection)
    if connection.execute('SELECT 1 FROM integrity_holds LIMIT 1').fetchone():
        issues['integrity_holds'] = 'RESET_ACTIVE_INTEGRITY_HOLD'
    for table in ('trade_intents', 'orders'):
        if connection.execute(f"SELECT 1 FROM {table} WHERE status NOT IN "
                              "('filled','canceled','expired','rejected','failed') LIMIT 1").fetchone():
            issues[table] = 'RESET_ORDER_NOT_TERMINAL'
    # Historical journal movements do not establish an opening cash balance.
    # A new disposable generation carries its own authoritative opening proof;
    # never assume a zero opening balance to unlock an unbound legacy ledger.
    opening = _opening(connection)
    if opening is None and any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
            for table in ('fills', 'settlements', 'position_lots', 'holdings', 'cash_ledger', 'pnl_records')):
        issues['cash_baseline'] = 'RESET_CASH_OPENING_EVIDENCE_MISSING'
    return {'issues': issues, 'digest': financial_evidence_digest(connection),
            'account_identity_digest': opening['account_identity_digest'] if opening else None}


def require_unlock_proof(connection, digest):
    proof = unlock_proof(connection)
    if proof['issues'] or proof['digest'] != digest:
        raise ValueError('RESET_UNLOCK_PROOF_CHANGED')
