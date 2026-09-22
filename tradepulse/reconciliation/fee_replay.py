"""Atomic restoration of derived FIFO accounting from complete broker history."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import (
    CashLedgerEntry,
    PnlRecord,
    ReconciliationOutcome,
    ReconciliationRecord,
    TradeAttribution,
    asset_identity_key,
)
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.settlement.engine import _infer_exit_reason, _parse_int_or_none

from .asset_fees import AssetFeeIntegrityError, AssetFeePending
from .cash_fees import allocate_cash_fees
from .fee_population import validate_fee_population
from .historical_fees import preview_fee_replay


async def replay_asset_fees(repositories, asset, activities, broker_quantity, *, now, lease_lost=None, pagination=None, evidence_hash=None):
    """Validate population and restate all derived rows in one DB transaction.

    The unfiltered activity history and current broker quantity are obtained by
    read-only calls before entry. Every local source is re-read under the write
    lock; concurrent fills or unfinished settlement invalidate the proof. Source
    fills, existing cash entries, order quantities/prices, and broker state are
    unchanged. Authoritative USD fee receipts create new immutable cash debits.
    """
    key = asset_identity_key(asset)
    if pagination is not None:
        from .activity_cursor import validate_pagination
        validate_pagination(activities, pagination)

    def apply(connection):
        if lease_lost is not None and lease_lost.is_set():
            raise AssetFeePending('ASSET_FEE_LEASE_LOST')
        def rows(table):
            return {row['record_id']: decode_payload(row['payload'])
                    for row in connection.execute(f'SELECT record_id,payload FROM {table}')}

        raw_fills, raw_events, raw_intents = rows('fills'), rows('settlements'), rows('trade_intents')
        fills = [hydrate('fills', row) for row in raw_fills.values()]
        intents = [hydrate('trade_intents', row) for row in raw_intents.values()]
        fees, proof = validate_fee_population(asset, activities, fills, intents, broker_quantity)
        if (any(fee.occurred_at > now for fee in fees)
                or any(datetime.fromisoformat(raw['created_at']) > now for raw in activities
                       if raw['id'] in proof.get('cash_fee_populations', {}))):
            raise AssetFeeIntegrityError('ASSET_FEE_FUTURE_TIMESTAMP')
        raw_lots = rows('position_lots')
        lots = [hydrate('position_lots', row) for row in raw_lots.values()]
        lots = [lot for lot in lots if asset_identity_key(lot.asset) == key]
        events = [hydrate('settlements', row) for row in raw_events.values()]
        events = [event for event in events if asset_identity_key(event.asset) == key]
        asset_fills = {fill.fill_id: fill for fill in fills if asset_identity_key(fill.asset) == key}
        if {event.fill_id for event in events} != asset_fills.keys():
            raise AssetFeePending('ASSET_FEE_SETTLEMENT_PENDING')
        for event in events:
            fill = asset_fills[event.fill_id]
            if event.status.value != 'completed' or not all((event.integrity_verified, event.lot_projected,
                    event.holding_projected, event.attribution_projected, event.cash_projected, event.trade_projected)):
                raise AssetFeePending('ASSET_FEE_SETTLEMENT_PENDING')
            if (event.quantity != fill.quantity or event.price != fill.price or event.side != fill.side
                    or event.occurred_at != fill.filled_at or event.trade_intent_id != fill.trade_intent_id
                    or event.broker_order_id != fill.order_id or event.execution_mode != fill.execution_mode
                    or event.broker_fill_id != fill.broker_fill_id or not event.broker_fill_id):
                raise AssetFeeIntegrityError('ASSET_FEE_SETTLEMENT_FILL_MISMATCH')
            if connection.execute('SELECT 1 FROM integrity_holds WHERE record_id=?', (event.broker_order_id,)).fetchone():
                raise AssetFeePending('ASSET_FEE_ORDER_VERIFICATION_PENDING')
        holding_row = connection.execute('SELECT payload FROM holdings WHERE record_id=?', (key,)).fetchone()
        holding = hydrate('holdings', decode_payload(holding_row['payload'])) if holding_row else None
        old_quantity = sum((lot.signed_quantity for lot in lots), Decimal(0))
        if (holding.quantity if holding else Decimal(0)) != old_quantity:
            raise AssetFeeIntegrityError('ASSET_FEE_HOLDING_LOT_MISMATCH')
        new_lots, realized = preview_fee_replay(lots, events, fees)
        quantity = sum((lot.signed_quantity for lot in new_lots), Decimal(0))
        if quantity != broker_quantity:
            raise AssetFeeIntegrityError('ASSET_FEE_REPLAY_BROKER_QUANTITY_MISMATCH')
        population_id = 'asset_fee_population:' + sha256(encode_payload(proof).encode()).hexdigest()
        from .epochs import epochs_for, save_epoch
        for epoch in epochs_for(connection, key):
            if epoch['fee_accounting_status'] == 'reconciled_net' and epoch.get('population_proof_id') != population_id:
                epoch['fee_accounting_status'] = 'fee_pending'
                epoch['reason'] = 'new_accounting_population'
                epoch['reconciled_at'] = None
                save_epoch(connection, epoch, now)

        ledgers = []
        allocation_versions = {}
        for fee in fees:
            allocations = {lot.lot_id: lot.asset_fee_quantities[fee.activity_id]
                           for lot in new_lots if fee.activity_id in lot.asset_fee_quantities}
            basis = sum((allocations.get(lot.lot_id, Decimal(0))*lot.acquisition_price for lot in new_lots), Decimal(0))
            ledger_id = 'asset_fee:' + fee.activity_id
            row = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?', (ledger_id,)).fetchone()
            if row:
                existing = decode_payload(row['payload'])['actual']
                if existing.get('activity') != dict(fee.receipt):
                    raise AssetFeeIntegrityError('ASSET_FEE_RECEIPT_CHANGED')
                if ({lid: Decimal(q) for lid, q in existing['allocations'].items()} != allocations
                        or Decimal(existing['cost_basis_debit']) != basis):
                    allocation = {'ledger_record_id': ledger_id, 'activity': fee.receipt, 'asset_key': key,
                                  'allocations': allocations, 'cost_basis_debit': basis,
                                  'population_record_id': population_id}
                    version_id = 'asset_fee_allocation:'+sha256(encode_payload(allocation).encode()).hexdigest()
                    allocation_versions[fee.activity_id] = version_id
                    if not connection.execute('SELECT 1 FROM reconciliation_records WHERE record_id=?', (version_id,)).fetchone():
                        ledgers.append(ReconciliationRecord(version_id, 'asset_fee', fee.activity_id,
                            ReconciliationOutcome.CORRECTED, expected={'original_ledger_record_id': ledger_id},
                            actual=allocation, occurred_at=now,
                            corrective_action='append corrected derived allocation; preserve original receipt and allocation'))
            else:
                ledgers.append(ReconciliationRecord(ledger_id, 'asset_fee', fee.activity_id, ReconciliationOutcome.CORRECTED,
                    expected={'quantity_debit': fee.quantity},
                    actual={'activity': fee.receipt, 'asset_key': key, 'allocations': allocations,
                            'cost_basis_debit': basis, 'holding_before': old_quantity, 'holding_after': quantity,
                            'population_record_id': population_id}, occurred_at=now,
                    corrective_action='authoritative instrument-population fee replay'))
        # Preserve the entire before image of each changed derived row in the
        # immutable correction receipt; no source execution is replaced.
        before = {}
        after = {}
        def change(table, identifier, old, new):
            encoded = decode_payload(encode_payload(new)) if new is not None else None
            if old != encoded:
                before.setdefault(table, {})[identifier] = old
                after.setdefault(table, {})[identifier] = encoded
        for lot in new_lots:
            change('position_lots', lot.lot_id, raw_lots[lot.lot_id], lot)
        by_intent = {intent.trade_intent_id: intent for intent in intents}
        by_event = {event.fill_id: event for event in events}
        raw_attrs = rows('trade_attributions')
        attrs = {identifier: row for identifier, row in raw_attrs.items() if row['lot_id'] in {lot.lot_id for lot in lots}}
        opportunities = rows('opportunities')
        desired_attrs = {}
        for lot in new_lots:
            opening = asset_fills[lot.originating_fill_id]
            intent = by_intent[opening.trade_intent_id]
            for fill_id, amount in lot.closures.items():
                event = by_event[fill_id]
                identifier = f'{lot.lot_id}:{fill_id}'
                pnl = (event.price-lot.acquisition_price)*amount
                if identifier in attrs:
                    old = hydrate('trade_attributions', attrs[identifier])
                    if (old.opening_trade_intent_id != opening.trade_intent_id or old.closing_trade_intent_id != event.trade_intent_id
                            or old.entry_price != lot.acquisition_price or old.exit_price != event.price
                            or old.lot_id != lot.lot_id or old.closing_fill_id != fill_id
                            or asset_identity_key(old.asset) != key or old.entry_at != lot.opened_at
                            or old.exit_at != event.occurred_at):
                        raise AssetFeeIntegrityError('ASSET_FEE_ATTRIBUTION_SOURCE_MISMATCH')
                    attribute = replace(old, quantity=amount, realized_pnl=pnl)
                else:
                    opportunity = opportunities.get(intent.correlation_id)
                    attribute = TradeAttribution(identifier, asset, lot.lot_id, intent.trade_intent_id, event.trade_intent_id,
                        fill_id, amount, lot.acquisition_price, lot.opened_at, event.price, event.occurred_at, pnl, now,
                        exit_reason=_infer_exit_reason('long', intent.stop_loss, intent.target_price, event.price,
                            held_days=(event.occurred_at.date()-lot.opened_at.date()).days,
                            max_hold_days=_parse_int_or_none(intent.risk_snapshot.get('max_hold_days'))),
                        entry_context={'risk_snapshot': dict(intent.risk_snapshot),
                                       'opportunity_metadata': opportunity.get('metadata') if opportunity else None})
                desired_attrs[identifier] = attribute
        for identifier in attrs.keys() | desired_attrs.keys():
            change('trade_attributions', identifier, attrs.get(identifier), desired_attrs.get(identifier))
        new_events = {}
        for event in events:
            # Realized P&L is derived; source fill economics and stage flags stay intact.
            new_event = replace(event, realized_pnl=realized[event.fill_id])
            new_events[event.fill_id] = new_event
            change('settlements', event.settlement_event_id, raw_events[event.settlement_event_id], new_event)
        for intent_id in {event.trade_intent_id for event in events}:
            intent = by_intent[intent_id]
            total = sum((event.realized_pnl for event in new_events.values() if event.trade_intent_id == intent_id), Decimal(0))
            change('trade_intents', intent_id, raw_intents[intent_id], replace(intent, realized_pnl=total))
        if holding:
            updated = None if quantity == 0 else replace(holding, quantity=quantity,
                average_price=sum((lot.remaining_quantity*lot.acquisition_price for lot in new_lots), Decimal(0))/quantity)
            # Keep updated_at stable on a verified no-op, but stamp a real correction.
            if updated is not None and (updated.quantity != holding.quantity or updated.average_price != holding.average_price):
                updated = replace(updated, updated_at=now)
            change('holdings', key, decode_payload(holding_row['payload']), updated)
        elif quantity:
            raise AssetFeeIntegrityError('ASSET_FEE_HOLDING_MISSING')
        cash_plans = allocate_cash_fees(proof, desired_attrs.values())
        cash_records = []
        cash_entries = []
        cash_allocation_versions = {}
        for raw, order, allocations in cash_plans:
            identifier = 'asset_cash_fee:' + raw['id']
            entry = CashLedgerEntry(identifier, 'alpaca:CFEE:USD:'+raw['id'], Decimal(raw['net_amount']), 'USD',
                                   datetime.fromisoformat(raw['created_at']), 'authoritative Alpaca USD crypto fee')
            row = connection.execute('SELECT payload FROM cash_ledger WHERE record_id=?', (identifier,)).fetchone()
            old = decode_payload(row['payload']) if row else None
            if old is not None and old != decode_payload(encode_payload(entry)):
                raise AssetFeeIntegrityError('CASH_FEE_LEDGER_RECEIPT_MISMATCH')
            if old is None:
                before.setdefault('cash_ledger', {})[identifier] = None
                after.setdefault('cash_ledger', {})[identifier] = decode_payload(encode_payload(entry))
                cash_entries.append(entry)
            actual = {'activity': raw, 'order': order, 'allocations': allocations,
                      'population_record_id': population_id, 'cash_entry_id': identifier,
                      'allocation_policy': 'verified_sell_population_proceeds_proportion',
                      'canonical_asset_key': key,
                      'accounting_epoch_ids': [e['accounting_epoch_id'] for e in epochs_for(connection, key)
                          if set(order['trade_intent_ids']) & set(e['trade_intent_ids'])]
                          or ['crypto_epoch:history:'+sha256(key.encode()).hexdigest()],
                      'broker_order_population': order['broker_order_ids'], 'intent_population': order['trade_intent_ids'],
                      'attribution_method': order['method'],
                      'allocation_precision': str(Decimal(1).scaleb(entry.amount.as_tuple().exponent)),
                      'final_remainder': next(reversed(allocations.values())),
                      'population_hash': population_id.split(':', 1)[1], 'idempotency_key': entry.idempotency_key}
            row = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?', (identifier,)).fetchone()
            if row:
                prior = decode_payload(row['payload'])['actual']
                if prior['activity'] != raw:
                    raise AssetFeeIntegrityError('CASH_FEE_RECEIPT_CHANGED')
                if prior['order'] != order or {k: Decimal(v) for k, v in prior['allocations'].items()} != allocations:
                    version_id = 'cash_fee_allocation:'+sha256(encode_payload(actual).encode()).hexdigest()
                    cash_allocation_versions[raw['id']] = version_id
                    if not connection.execute('SELECT 1 FROM reconciliation_records WHERE record_id=?', (version_id,)).fetchone():
                        cash_records.append(ReconciliationRecord(version_id, 'asset_fee', raw['id'], ReconciliationOutcome.CORRECTED,
                            expected={'original_ledger_record_id': identifier}, actual=actual, occurred_at=now,
                            corrective_action='append corrected cash expense allocation; original receipt and debit unchanged'))
            else:
                cash_records.append(ReconciliationRecord(identifier, 'asset_fee', raw['id'], ReconciliationOutcome.CORRECTED,
                    expected={'currency': 'USD', 'expense': -entry.amount}, actual=actual, occurred_at=now,
                    corrective_action='record USD fee once; allocate exhaustive verified sell-population expense'))
        # Canonical PnL is a derived projection, restated atomically with the
        # attributions and retained in the same immutable before/after receipt.
        raw_pnl = rows('pnl_records')
        desired_pnl = { 'fill:pnl:' + identifier: PnlRecord('fill:pnl:' + identifier, asset,
            attr.realized_pnl, Decimal(0), attr.exit_at) for identifier, attr in desired_attrs.items() }
        for fee in fees:
            basis = sum((lot.asset_fee_quantities.get(fee.activity_id, Decimal(0))*lot.acquisition_price
                         for lot in new_lots), Decimal(0))
            identifier = 'asset_fee:pnl:' + fee.activity_id
            desired_pnl[identifier] = PnlRecord(identifier, asset, -basis, Decimal(0), fee.occurred_at)
        for raw, _, _ in cash_plans:
            identifier = 'asset_cash_fee:pnl:' + raw['id']
            desired_pnl[identifier] = PnlRecord(identifier, asset, Decimal(raw['net_amount']), Decimal(0),
                                               datetime.fromisoformat(raw['created_at']))
        existing_pnl = {identifier: raw for identifier, raw in raw_pnl.items()
                        if asset_identity_key(hydrate('pnl_records', raw).asset) == key
                        and identifier.startswith(('fill:pnl:', 'asset_fee:pnl:', 'asset_cash_fee:pnl:'))}
        for identifier in existing_pnl.keys() | desired_pnl.keys():
            change('pnl_records', identifier, existing_pnl.get(identifier), desired_pnl.get(identifier))

        def record(item):
            connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                               (item.record_id, encode_payload(item), now.isoformat()))
        if before or ledgers:
            revision = sha256(encode_payload({'before': before, 'after': after, 'population': population_id}).encode()).hexdigest()
            correction = ReconciliationRecord('asset_fee_replay:'+revision, 'asset_fee', key, ReconciliationOutcome.CORRECTED,
                expected={'source_fills_unchanged': True},
                actual={'population_record_id': population_id, 'before': before, 'after': after,
                        'evidence_file_sha256': evidence_hash, 'broker_activity_ids': sorted(raw['id'] for raw in activities)}, occurred_at=now,
                corrective_action='atomic replay of lot closures, attributions, realized summaries and holding')
            record(correction)
        if lease_lost is not None and lease_lost.is_set():
            raise AssetFeePending('ASSET_FEE_LEASE_LOST')
        for table, changes in after.items():
            for identifier, payload in changes.items():
                if table == 'cash_ledger':
                    continue  # immutable entries are inserted below with their idempotency keys
                if payload is None:
                    connection.execute(f'DELETE FROM {table} WHERE record_id=?', (identifier,))
                elif before[table][identifier] is None:
                    if table not in {'trade_attributions', 'pnl_records'}:
                        raise AssetFeeIntegrityError('ASSET_FEE_UNEXPECTED_NEW_DERIVED_ROW')
                    connection.execute(f'INSERT INTO {table}(record_id,payload,created_at) VALUES(?,?,?)',
                                       (identifier, encode_payload(payload), now.isoformat()))
                else:
                    # Existing status/identity columns are unchanged by replay.
                    sql = f'UPDATE {table} SET payload=?'
                    values = [encode_payload(payload)]
                    if table not in {'trade_attributions', 'pnl_records'}:
                        sql += ',updated_at=?'
                        values.append(now.isoformat())
                    connection.execute(sql+' WHERE record_id=?', (*values, identifier))
        record(ReconciliationRecord(population_id, 'asset_fee', key+':population', ReconciliationOutcome.MATCHED,
            expected={'complete_instrument_population': True}, actual=proof, occurred_at=now))
        for ledger in ledgers:
            record(ledger)
        for entry in cash_entries:
            connection.execute('INSERT INTO cash_ledger(record_id,idempotency_key,payload,created_at) VALUES(?,?,?,?)',
                               (entry.entry_id, entry.idempotency_key, encode_payload(entry), now.isoformat()))
        for item in cash_records:
            record(item)
        for raw, _, _ in cash_plans:
            record(ReconciliationRecord(f'asset_cash_fee_membership:{raw["id"]}:{population_id}', 'asset_fee', raw['id'],
                ReconciliationOutcome.MATCHED, expected={'population_verified': True},
                actual={'ledger_record_id': 'asset_cash_fee:'+raw['id'], 'population_record_id': population_id,
                        'allocation_record_id': cash_allocation_versions.get(raw['id'])}, occurred_at=now))
        # Stable membership receipts also upgrade pre-existing Rev.103 ledgers
        # without rewriting their immutable original fee observations.
        for fee in fees:
            identifier = f'asset_fee_membership:{fee.activity_id}:{population_id}'
            record(ReconciliationRecord(identifier, 'asset_fee', fee.activity_id, ReconciliationOutcome.MATCHED,
                expected={'population_verified': True}, actual={'ledger_record_id': 'asset_fee:'+fee.activity_id,
                    'population_record_id': population_id,
                    'allocation_record_id': allocation_versions.get(fee.activity_id)}, occurred_at=now))
        from .epochs import finalize_population
        finalize_population(connection, key=key, proof=proof, population_id=population_id,
                            fills=list(asset_fills.values()), fees=fees, cash_plans=cash_plans,
                            lots=new_lots, quantity=quantity, activities=activities, now=now,
                            pagination=pagination, evidence_hash=evidence_hash)
        return bool(before or ledgers or cash_records)

    return await repositories.position_lots.database.run(apply, write=True)
