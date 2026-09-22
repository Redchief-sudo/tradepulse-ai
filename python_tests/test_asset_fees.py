"""Isolated regression fixtures reproduce the receipt quantities, never an account."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    Holding,
    PositionLot,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, DatabaseError, PersistenceRepositories, hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.reconciliation.asset_fees import (
    AssetFeeIntegrityError,
    AssetFeePending,
    parse_asset_fee,
    reconcile_asset_fees,
)
from tradepulse.risk import load_session

FIRST = datetime(2026, 9, 3, 19, 35, 11, tzinfo=UTC)
NOW = datetime(2026, 9, 17, tzinfo=UTC)
ASSET = AssetIdentity('SOL/USD', AssetClass.CRYPTO, 'alpaca:SOL/USD')
KEY = asset_identity_key(ASSET)


def receipt(identifier='fee-1', quantity='-0.006890872', at='2026-09-03T19:35:14.115340Z'):
    return {'id': identifier, 'activity_type': 'CFEE', 'status': 'executed', 'symbol': 'SOLUSD',
            'description': 'Coin Pair Transaction Fee (Non USD)', 'qty': quantity, 'net_amount': '0',
            'created_at': at, 'date': '2026-09-03', 'currency': 'USD', 'price': '105.35'}


def four_receipts():
    return [receipt(), receipt('fee-2', '-0.03477075'),
            receipt('fee-3', '-0.035043', '2026-09-03T21:26:10.662719Z'),
            receipt('fee-4', '-0.015104457', '2026-09-03T21:26:10.662719Z')]


async def seeded(tmp_path):
    db = AsyncSQLiteDatabase(f'sqlite:///{tmp_path}/fees.db')
    await db.initialize()
    r = PersistenceRepositories.create(db)
    cost, total = D(0), D(0)
    for i, (q, p) in enumerate([('13.9083', '105.3'), ('2.75634854', '105.353'), ('14.0172', '105.1'), ('6.04178279', '105.103')]):
        stamp = FIRST + (timedelta(hours=1, minutes=50) if i >= 2 else timedelta(0)) + timedelta(microseconds=i)
        fid = f'fill-{i}'
        f = Fill(fid, f'intent-{i//2}', f'order-{i//2}', ASSET, Side.BUY, ExecutionMode.PAPER, D(q), D(p), D(0), D(0), stamp, fid)
        await r.fills.create_once(fid, f, unique_value=fid)
        e = SettlementEvent(fid, fid, f.trade_intent_id, ASSET, Side.BUY, ExecutionMode.PAPER, D(q), D(p), stamp,
                            status=SettlementStatus.COMPLETED, broker_order_id=f.order_id, broker_fill_id=fid,
                            lot_projected=True, attribution_projected=True, cash_projected=True,
                            holding_projected=True, trade_projected=True, integrity_verified=True, realized_pnl=D(0))
        await r.settlements.create_once(fid, e, status='completed', unique_value=fid)
        lot = PositionLot(f'lot-{i}', fid, ASSET, 'long', D(q), D(q), D(p), stamp)
        await r.position_lots.create_once(lot.lot_id, lot, unique_value=fid)
        total += D(q)
        cost += D(q)*D(p)
    for group in range(2):
        intent = TradeIntent(f'intent-{group}', f'key-{group}', f'op-{group}', ASSET, Side.BUY,
                             ExecutionMode.PAPER, 'test', FIRST, requested_quantity=D('16.66464854') if group == 0 else D('20.05898279'),
                             filled_quantity=D('16.66464854') if group == 0 else D('20.05898279'),
                             status=TradeIntentStatus.FILLED, broker_order_id=f'order-{group}', realized_pnl=D(0))
        await r.trade_intents.create_once(intent.trade_intent_id, intent, status='filled', unique_value=intent.idempotency_key)
    h = Holding(ASSET, total, cost/total, NOW, sector='Crypto', stop_loss=D(94), current_stop=D('101.123456789'))
    await r.holdings.create_once(KEY, h)
    return r


async def population(r, fee_receipts):
    fills = [hydrate('fills', row['payload']) for row in await r.fills.list_all()]
    raw = [{'id': f.broker_fill_id, 'activity_type': 'FILL', 'symbol': f.asset.symbol,
            'order_id': f.order_id, 'side': f.side.value, 'qty': str(f.quantity), 'price': str(f.price),
            'transaction_time': f.filled_at.isoformat()} for f in fills]
    raw += list(fee_receipts)
    quantity = sum((f.quantity if f.side == Side.BUY else -f.quantity for f in fills), D(0))
    quantity -= sum((parse_asset_fee(x).quantity for x in fee_receipts), D(0))
    return raw, quantity


async def apply_asset_fee(r, fee, *, now):
    # Tests supply an explicit complete synthetic population; production never
    # manufactures these receipts or derives a broker quantity from local lots.
    existing = {row['payload']['subject_id']: row['payload']['actual']['activity']
                for row in await r.reconciliation_records.list_all()
                if row['record_id'].startswith('asset_fee:')}
    existing[fee.activity_id] = dict(fee.receipt)
    raw, quantity = await population(r, existing.values())
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    if parse_asset_fee(fee.receipt) != fee:
        raise AssetFeeIntegrityError('ASSET_FEE_RECEIPT_FIELDS_MISMATCH')
    return await replay_asset_fees(r, fee.asset, raw, quantity, now=now)


async def fee_ledgers(r):
    return [row for row in await r.reconciliation_records.list_all() if row['record_id'].startswith('asset_fee:')]


async def fee_broker(r, fee_receipts):
    raw, quantity = await population(r, fee_receipts)
    broker = AsyncMock()
    async def activities(**kwargs):
        selected = raw
        if kwargs.get('after_id'):
            selected = [row for row in raw if row['id'] > kwargs['after_id']]
        pages = kwargs.get('page_evidence')
        if pages is not None:
            from hashlib import sha256
            pages.append({'request': {'page_size': '100', 'direction': 'asc'},
                          'activity_ids': [row['id'] for row in selected], 'terminal': True,
                          'response_hash': sha256(encode_payload(selected).encode()).hexdigest()})
        return [SimpleNamespace(raw=row, activity_id=row['id']) for row in selected]
    broker.get_activities.side_effect = activities
    broker.activity_implementation = activities
    broker.get_positions.return_value = [] if quantity == 0 else [SimpleNamespace(
        asset_class=AssetClass.CRYPTO, symbol='SOL/USD', qty=quantity)]
    return broker


async def test_four_authoritative_fees_explain_sol_exactly_and_replay_once(tmp_path):
    r = await seeded(tmp_path)
    original_fills = await r.fills.list_all()
    original_settlements = await r.settlements.list_all()
    for raw in four_receipts():
        assert await apply_asset_fee(r, parse_asset_fee(raw), now=NOW)
    h = hydrate('holdings', (await r.holdings.get(KEY))['payload'])
    lots = [hydrate('position_lots', row['payload']) for row in await r.position_lots.list_all()]
    from tradepulse.settlement.lots import plan_signed_lot_fill
    for row in original_settlements:
        plan = plan_signed_lot_fill(lots, hydrate('settlements', row['payload']))
        assert plan.opening_quantity == 0  # replay cannot recreate units already debited as fees
        assert plan.closures == []
    assert h.quantity == D('36.631822251')
    assert sum((lot.remaining_quantity for lot in lots), D(0)) == h.quantity
    assert sum((sum(lot.asset_fee_quantities.values(), D(0)) for lot in lots), D(0)) == D('0.091809079')
    assert h.stop_loss == D(94) and h.current_stop == D('101.123456789')
    before = (await r.holdings.get(KEY), await r.position_lots.list_all())
    for raw in four_receipts():
        assert not await apply_asset_fee(r, parse_asset_fee(raw), now=NOW)
    assert (await r.holdings.get(KEY), await r.position_lots.list_all()) == before
    assert len(await fee_ledgers(r)) == 4
    assert await r.fills.list_all() == original_fills
    assert await r.settlements.list_all() == original_settlements


async def test_concurrent_replay_debits_once(tmp_path):
    r = await seeded(tmp_path)
    fee = parse_asset_fee(receipt())
    results = await asyncio.gather(apply_asset_fee(r, fee, now=NOW), apply_asset_fee(r, fee, now=NOW))
    assert sorted(results) == [False, True]
    assert len(await fee_ledgers(r)) == 1
    assert D((await r.holdings.get(KEY))['payload']['quantity']) == D('36.72363133')-fee.quantity


async def test_failed_ledger_insert_rolls_back_lots_and_holding(tmp_path):
    r = await seeded(tmp_path)
    before = (await r.holdings.get(KEY), await r.position_lots.list_all())
    await r.position_lots.database.run(lambda c: c.execute("CREATE TRIGGER reject_fee BEFORE INSERT ON reconciliation_records BEGIN SELECT RAISE(ABORT,'forced rollback'); END"), write=True)
    with pytest.raises(DatabaseError, match='forced rollback'):
        await apply_asset_fee(r, parse_asset_fee(receipt()), now=NOW)
    assert (await r.holdings.get(KEY), await r.position_lots.list_all()) == before
    assert await r.reconciliation_records.list_all() == []


async def test_modified_receipt_id_cannot_change_previously_applied_amount(tmp_path):
    r = await seeded(tmp_path)
    await apply_asset_fee(r, parse_asset_fee(receipt()), now=NOW)
    before = await r.position_lots.list_all()
    with pytest.raises(AssetFeeIntegrityError, match='RECEIPT_CHANGED'):
        await apply_asset_fee(r, parse_asset_fee(receipt(quantity='-0.2')), now=NOW)
    assert await r.position_lots.list_all() == before


async def test_incomplete_late_sale_defers_historical_replay(tmp_path):
    r = await seeded(tmp_path)
    fill = Fill('sell', 'sell-intent', 'sell-order', ASSET, Side.SELL, ExecutionMode.PAPER, D(1), D(110), D(0), D(0), NOW, 'sell')
    await r.fills.create_once('sell', fill, unique_value='sell')
    intent = TradeIntent('sell-intent', 'sell-key', 'op-sell', ASSET, Side.SELL, ExecutionMode.PAPER,
                         'test', NOW, requested_quantity=D(1), status=TradeIntentStatus.FILLED,
                         broker_order_id='sell-order')
    await r.trade_intents.create_once(intent.trade_intent_id, intent, status='filled', unique_value=intent.idempotency_key)
    lot = hydrate('position_lots', (await r.position_lots.get('lot-0'))['payload'])
    await r.position_lots.update('lot-0', replace(lot, remaining_quantity=lot.remaining_quantity-1, closures={'sell': D(1)}))
    before = (await r.holdings.get(KEY), await r.position_lots.list_all())
    with pytest.raises(AssetFeeIntegrityError, match='SETTLEMENT_PENDING'):
        await apply_asset_fee(r, parse_asset_fee(receipt()), now=NOW)
    assert (await r.holdings.get(KEY), await r.position_lots.list_all()) == before


async def test_pending_settlement_defers_fee_without_partial_mutation(tmp_path):
    r = await seeded(tmp_path)
    e = hydrate('settlements', (await r.settlements.get('fill-0'))['payload'])
    await r.settlements.update('fill-0', replace(e, status=SettlementStatus.PROCESSING), status='processing')
    before = await r.position_lots.list_all()
    with pytest.raises(AssetFeePending, match='SETTLEMENT_PENDING'):
        await apply_asset_fee(r, parse_asset_fee(receipt()), now=NOW)
    assert await r.position_lots.list_all() == before


@pytest.mark.parametrize('change', [{'qty': 'NaN'}, {'qty': '0'}, {'qty': '1'}, {'net_amount': '-1'},
                                   {'created_at': '2026-09-03'}, {'symbol': 'SOL'}, {'status': 'pending'}, {'activity_type': 'FILL'}])
def test_unknown_or_malformed_receipts_are_not_invented_debits(change):
    with pytest.raises((ValueError, KeyError)):
        parse_asset_fee({**receipt(), **change})


async def test_reconciliation_ingests_created_at_order_not_activity_id_order(tmp_path):
    r = await seeded(tmp_path)
    broker = await fee_broker(r, list(reversed(four_receipts())))
    assert await reconcile_asset_fees(r, broker, now=NOW)
    assert D((await r.holdings.get(KEY))['payload']['quantity']) == D('36.631822251')
    assert broker.get_activities.call_args.kwargs['activity_type'] is None


async def test_reconciliation_failure_is_persisted_and_latched(tmp_path):
    r = await seeded(tmp_path)
    broker = await fee_broker(r, [receipt(quantity='-100')])
    before = await r.position_lots.list_all()
    assert not await reconcile_asset_fees(r, broker, now=NOW)
    assert await r.position_lots.list_all() == before
    assert not (await load_session(r)).financial_integrity_manual_reenable_required
    assert (await r.accounting_epochs.list_all())[0]['status'] == 'integrity_blocked'
    records = await r.reconciliation_records.list_all()
    assert records[0]['payload']['outcome'] == 'drift_detected'


@pytest.mark.parametrize('tamper', [None, 'missing', 'amount', 'asset', 'cash', 'cash_missing', 'cash_amount'])
def test_prove_edge_conserves_fee_units_and_expenses_basis_once(tamper):
    from python_tests.test_paper_verification import COSTS, START, passing_rows
    from python_tests.test_paper_verification import NOW as ASSESS_NOW
    from tradepulse.persistence.codec import decode_payload, encode_payload
    from tradepulse.verification.evidence import assess

    rows = passing_rows(count=1, wins=1)
    asset = decode_payload(encode_payload(ASSET))
    for table in ['fills', 'settlements', 'position_lots', 'trade_attributions', 'trade_intents']:
        for row in rows[table]:
            row['asset'] = asset
    rows['fills'][1]['quantity'] = '0.9'
    rows['settlements'][1]['quantity'] = '0.9'
    lot = rows['position_lots'][0]
    lot.update(closures={'s0': '0.9'}, asset_fee_quantities={'fee-test': '0.1'}, realized_pnl='1.8')
    rows['trade_attributions'][0].update(quantity='0.9', realized_pnl='1.8')
    rows['reconciliation_records'][0]['subject_id'] = KEY
    raw = receipt('fee-test', '-0.1', (START+timedelta(days=2)).isoformat())
    record = {'record_id': 'asset_fee:fee-test', 'subject_id': 'fee-test', 'reconciliation_type': 'asset_fee',
              'outcome': 'corrected', 'occurred_at': ASSESS_NOW.isoformat(),
              'actual': {'activity': raw, 'asset_key': KEY, 'allocations': {'lot0': '0.1'}, 'cost_basis_debit': '10'}}
    from hashlib import sha256

    from tradepulse.reconciliation.fee_population import validate_fee_population
    opening = rows['trade_intents'][0]
    opening['broker_order_id'] = rows['fills'][0]['order_id']
    closing = {**opening, 'trade_intent_id': 'exit0', 'broker_order_id': rows['fills'][1]['order_id'],
               'side': 'sell', 'filled_quantity': '0.9'}
    rows['trade_intents'] = [i for i in rows['trade_intents'] if i['trade_intent_id'] != closing['trade_intent_id']] + [closing]
    activities = [{'id': f['broker_fill_id'], 'activity_type': 'FILL', 'symbol': 'SOL/USD',
                   'order_id': f['order_id'], 'side': f['side'], 'qty': f['quantity'], 'price': f['price'],
                   'transaction_time': f['filled_at']} for f in rows['fills']] + [raw]
    if tamper in ('cash', 'cash_missing', 'cash_amount'):
        cash = {**cash_receipt('cash-test', '-0.09'), 'created_at': (ASSESS_NOW-timedelta(minutes=1)).isoformat()}
        activities.append(cash)
    _, proof = validate_fee_population(ASSET, activities, [hydrate('fills', f) for f in rows['fills']],
                                      [hydrate('trade_intents', i) for i in rows['trade_intents']], D(0))
    pop_id = 'asset_fee_population:' + sha256(encode_payload(proof).encode()).hexdigest()
    rows['reconciliation_records'].append({'record_id': pop_id, 'subject_id': KEY+':population',
        'reconciliation_type': 'asset_fee', 'outcome': 'matched', 'occurred_at': ASSESS_NOW.isoformat(), 'actual': decode_payload(encode_payload(proof))})
    record['actual']['population_record_id'] = pop_id
    from python_tests.test_accounting_epochs import pagination
    from tradepulse.reconciliation.epochs import new_epoch
    epoch = new_epoch(KEY, 'test-epoch', START, D(0), [opening['trade_intent_id'], closing['trade_intent_id']])
    epoch.update(fee_accounting_status='reconciled_net', population_proof_id=pop_id,
                 population_hash=pop_id.split(':', 1)[1], pagination_proof=pagination(activities),
                 fill_ids=[f['fill_id'] for f in rows['fills']], checkpoint_version=1,
                 net_inventory_quantity='0', ending_broker_quantity='0',
                 conservation={'starting_broker_quantity': '0', 'buys': '1', 'sells': '0.9',
                               'asset_fees': '0.1', 'ending_broker_quantity': '0'})
    epoch['checkpoint_id'] = 'epoch_checkpoint:'+sha256(encode_payload({
        'epoch': 'test-epoch', 'version': 1, 'population': pop_id}).encode()).hexdigest()
    rows['accounting_epochs'] = [epoch]
    rows['reconciliation_records'].append({'record_id': epoch['checkpoint_id'], 'subject_id': 'test-epoch',
        'reconciliation_type': 'asset_fee', 'outcome': 'matched', 'occurred_at': ASSESS_NOW.isoformat(), 'actual': epoch})
    if tamper in ('cash', 'cash_amount'):
        from tradepulse.models import CashLedgerEntry
        from tradepulse.reconciliation.cash_fees import allocate_cash_fees
        _, order, allocations = allocate_cash_fees(proof, [hydrate('trade_attributions', a) for a in rows['trade_attributions']])[0]
        cash_id = 'asset_cash_fee:cash-test'
        rows['reconciliation_records'].append({'record_id': cash_id, 'subject_id': 'cash-test',
            'reconciliation_type': 'asset_fee', 'outcome': 'corrected', 'occurred_at': ASSESS_NOW.isoformat(),
            'actual': decode_payload(encode_payload({'activity': cash, 'order': order, 'allocations': allocations,
                'allocation_policy': 'verified_sell_population_proceeds_proportion', 'population_record_id': pop_id,
                'cash_entry_id': cash_id}))})
        entry = CashLedgerEntry(cash_id, 'alpaca:CFEE:USD:cash-test', D('-0.09') if tamper == 'cash' else D('-1'),
                                'USD', datetime.fromisoformat(cash['created_at']), 'test')
        rows['cash_ledger'].append(decode_payload(encode_payload(entry)))
    if tamper == 'amount':
        record['actual']['cost_basis_debit'] = '0'
    if tamper == 'asset':
        record['actual']['activity']['symbol'] = 'BTCUSD'
        record['actual']['asset_key'] = 'crypto:default:alpaca:BTC/USD'
    if tamper != 'missing':
        rows['reconciliation_records'].append(record)
    # Keep canonical journal evidence aligned with this changed crypto fixture.
    for fill in rows['fills']:
        entry = next(e for e in rows['cash_ledger'] if e['entry_id'] == 'fill:cash:' + fill['fill_id'])
        entry['amount'] = str(D(fill['quantity'])*D(fill['price'])*(1 if fill['side'] == 'sell' else -1))
    for pnl in rows['pnl_records']:
        attr = next(a for a in rows['trade_attributions'] if pnl['record_id'] == 'fill:pnl:' + a['attribution_id'])
        pnl.update(asset=asset, realized=attr['realized_pnl'])
    result = assess(rows, START.isoformat(), ASSESS_NOW, COSTS)
    if tamper in (None, 'cash'):
        assert result['criteria']['eligible_round_trips']['actual'] == 1
        assert D(result['criteria']['net_realized_pnl']['actual']) == D('-8.23836') - (D('.09') if tamper == 'cash' else D(0))  # -8.2 actual result, less the unchanged 2 bps model on 191.8 notional
        assert result['criteria']['evidence_errors']['actual'] == 0
    else:
        assert result['status'] == 'PROVE_EDGE_FAILED_INTEGRITY'
        assert result['population'] == []


async def test_unchanged_fee_polls_do_not_grow_evidence_and_recovery_is_recorded(tmp_path):
    r = await seeded(tmp_path)
    broker = await fee_broker(r, four_receipts())
    assert await reconcile_asset_fees(r, broker, now=NOW)
    count = len(await r.reconciliation_records.list_all())
    for _ in range(4):
        assert await reconcile_asset_fees(r, broker, now=NOW)
    assert len(await r.reconciliation_records.list_all()) == count
    broker.get_activities.side_effect = RuntimeError('unavailable')
    for _ in range(3):
        assert not await reconcile_asset_fees(r, broker, now=NOW)
    assert len(await r.reconciliation_records.list_all()) == count+3
    broker.get_activities.side_effect = broker.activity_implementation
    assert await reconcile_asset_fees(r, broker, now=NOW)
    assert len(await r.reconciliation_records.list_all()) == count+6


async def test_historical_preview_reallocates_partial_sales_and_conserves_fills(tmp_path):
    from tradepulse.reconciliation.historical_fees import preview_fee_replay
    r = await seeded(tmp_path)
    lots = [hydrate('position_lots', row['payload']) for row in await r.position_lots.list_all()]
    events = [hydrate('settlements', row['payload']) for row in await r.settlements.list_all()]
    template = events[0]
    sale1 = replace(template, settlement_event_id='sell1', fill_id='sell1', broker_fill_id='sell1', side=Side.SELL,
                    quantity=D('13.9225'), price=D('101.55'), occurred_at=NOW)
    sale2 = replace(sale1, settlement_event_id='sell2', fill_id='sell2', broker_fill_id='sell2', quantity=D('22.709322251'),
                    price=D('101.522'), occurred_at=NOW+timedelta(microseconds=1))
    events += [sale1, sale2]
    gross_lots, _ = preview_fee_replay(lots, events, [])
    assert sum((x.remaining_quantity for x in gross_lots), D(0)) == D('0.091809079')
    corrected, realized = preview_fee_replay(gross_lots, events, [parse_asset_fee(x) for x in four_receipts()])
    assert all(x.remaining_quantity == 0 for x in corrected)
    for sale in [sale1, sale2]:
        assert sum((x.closures.get(sale.fill_id, D(0)) for x in corrected), D(0)) == sale.quantity
    fee_basis = sum((sum(x.asset_fee_quantities.values(), D(0))*x.acquisition_price for x in corrected), D(0))
    proceeds = sale1.quantity*sale1.price+sale2.quantity*sale2.price
    purchase_cost = sum((x.opened_quantity*x.acquisition_price for x in corrected), D(0))
    assert sum(realized.values(), D(0))-fee_basis == proceeds-purchase_cost
    assert preview_fee_replay(corrected, events, [parse_asset_fee(x) for x in four_receipts()]) == (corrected, realized)
    with pytest.raises(AssetFeeIntegrityError, match='REPLAY_RECEIPT_MISSING'):
        preview_fee_replay(corrected, events, [])
    with pytest.raises(AssetFeeIntegrityError, match='REPLAY_OVERSELL'):
        preview_fee_replay(gross_lots, [*events[:-1], replace(sale2, quantity=D(100))],
                           [parse_asset_fee(x) for x in four_receipts()])
    # Preview has no persistence side effects.
    assert [hydrate('position_lots', row['payload']) for row in await r.position_lots.list_all()] == lots


async def test_fee_specific_failure_has_one_recovery_transition(tmp_path):
    r = await seeded(tmp_path)
    broker = await fee_broker(r, [receipt()])
    assert await reconcile_asset_fees(r, broker, now=NOW)
    bad = await fee_broker(r, [receipt(quantity='-0.2')])
    assert not await reconcile_asset_fees(r, bad, now=NOW)
    for _ in range(3):
        assert await reconcile_asset_fees(r, broker, now=NOW)
    rows = [r['payload'] for r in await r.reconciliation_records.list_all()]
    feed = [r for r in rows if r['subject_id'] == 'asset_fee_feed:'+KEY]
    assert [r['outcome'] for r in feed] == ['matched', 'drift_detected', 'matched']


async def test_receipt_without_population_cannot_mutate(tmp_path):
    r = await seeded(tmp_path)
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    with pytest.raises(AssetFeeIntegrityError, match='INCOMPLETE_FILL_HISTORY'):
        await replay_asset_fees(r, ASSET, [receipt()], D(0), now=NOW)


async def sold_history(r):
    from tradepulse.reconciliation.historical_fees import preview_fee_replay
    from tradepulse.settlement.engine import _project_attribution
    lots = [hydrate('position_lots', row['payload']) for row in await r.position_lots.list_all()]
    events = [hydrate('settlements', row['payload']) for row in await r.settlements.list_all()]
    intent = TradeIntent('sell-intent', 'sell-key', 'exit-op', ASSET, Side.SELL, ExecutionMode.PAPER,
                         'test', NOW, requested_quantity=D('36.631822251'), status=TradeIntentStatus.FILLED,
                         broker_order_id='sell-order', filled_quantity=D('36.631822251'))
    await r.trade_intents.create_once(intent.trade_intent_id, intent, status='filled', unique_value=intent.idempotency_key)
    sales = []
    for i, (q, price) in enumerate([('13.9225', '101.55'), ('22.709322251', '101.522')]):
        fill = Fill(f'sell-{i}', 'sell-intent', 'sell-order', ASSET, Side.SELL, ExecutionMode.PAPER,
                    D(q), D(price), D(0), D(0), NOW+timedelta(microseconds=i), f'sell-{i}')
        await r.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)
        event = replace(events[0], settlement_event_id=fill.fill_id, fill_id=fill.fill_id,
                        trade_intent_id='sell-intent', broker_order_id='sell-order', broker_fill_id=fill.fill_id,
                        side=Side.SELL, quantity=fill.quantity, price=fill.price, occurred_at=fill.filled_at)
        events.append(event)
        sales.append(event)
    gross, realized = preview_fee_replay(lots, events, [])
    for lot in gross:
        await r.position_lots.update(lot.lot_id, lot)
    for event in sales:
        event = replace(event, realized_pnl=realized[event.fill_id])
        await r.settlements.create_once(event.settlement_event_id, event, status='completed', unique_value=event.fill_id)
        await _project_attribution(r, event)
    intent = replace(intent, realized_pnl=sum(realized.values(), D(0)))
    await r.trade_intents.update(intent.trade_intent_id, intent, status='filled')
    holding = hydrate('holdings', (await r.holdings.get(KEY))['payload'])
    await r.holdings.update(KEY, replace(holding, quantity=D('0.091809079'), average_price=D('105.103')))
    return sales


async def test_atomic_post_sale_replay_updates_every_derived_result_once(tmp_path):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    r = await seeded(tmp_path)
    sales = await sold_history(r)
    raw, qty = await population(r, four_receipts())
    assert qty == 0
    before_fills = await r.fills.list_all()
    before_cash = await r.cash_ledger.list_all()
    stamp = NOW+timedelta(seconds=1)
    results = await asyncio.gather(*(replay_asset_fees(r, ASSET, raw, qty, now=stamp) for _ in range(2)))
    assert sorted(results) == [False, True]
    assert await r.holdings.get(KEY) is None
    lots = [hydrate('position_lots', x['payload']) for x in await r.position_lots.list_all()]
    attrs = [hydrate('trade_attributions', x['payload']) for x in await r.trade_attributions.list_all()]
    for sale in sales:
        amount = sum((lot.closures.get(sale.fill_id, D(0)) for lot in lots), D(0))
        assert amount == sale.quantity
        pnl = sum((a.realized_pnl for a in attrs if a.closing_fill_id == sale.fill_id), D(0))
        assert D((await r.settlements.get(sale.fill_id))['payload']['realized_pnl']) == pnl
    gross = sum((lot.realized_pnl for lot in lots), D(0))
    assert gross == D('-134.157609727268')
    assert D((await r.trade_intents.get('sell-intent'))['payload']['realized_pnl']) == gross
    fee_basis = sum((sum(lot.asset_fee_quantities.values(), D(0))*lot.acquisition_price for lot in lots), D(0))
    assert gross-fee_basis == D('-143.825105745968')
    assert await r.fills.list_all() == before_fills
    assert await r.cash_ledger.list_all() == before_cash
    records = [x['payload'] for x in await r.reconciliation_records.list_all()]
    corrections = [x for x in records if x['record_id'].startswith('asset_fee_replay:')]
    assert len(corrections) == 1
    assert 'trade_attributions' in corrections[0]['actual']['before']
    assert 'settlements' in corrections[0]['actual']['after']
    count = len(records)
    for _ in range(3):
        assert not await replay_asset_fees(r, ASSET, raw, qty, now=stamp+timedelta(days=1))
    assert len(await r.reconciliation_records.list_all()) == count


async def test_post_sale_transaction_rolls_back_after_lot_updates(tmp_path):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    r = await seeded(tmp_path)
    await sold_history(r)
    tables = [r.position_lots, r.trade_attributions, r.settlements, r.trade_intents, r.holdings, r.reconciliation_records]
    before = [await table.list_all() for table in tables]
    await r.position_lots.database.run(lambda c: c.execute(
        "CREATE TRIGGER reject_restatement BEFORE UPDATE ON trade_attributions BEGIN SELECT RAISE(ABORT,'forced late rollback'); END"), write=True)
    raw, qty = await population(r, four_receipts())
    with pytest.raises(DatabaseError, match='forced late rollback'):
        await replay_asset_fees(r, ASSET, raw, qty, now=NOW+timedelta(seconds=1))
    assert [await table.list_all() for table in tables] == before


@pytest.mark.parametrize('problem', ['external_fill', 'missing_fill', 'unknown_transfer', 'broker_quantity'])
async def test_population_ambiguity_cannot_debit_local_lots(tmp_path, problem):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    if problem == 'external_fill':
        raw.append({**raw[0], 'id': 'external-fill', 'order_id': 'manual-order'})
    elif problem == 'missing_fill':
        raw = raw[1:]
    elif problem == 'unknown_transfer':
        raw.append({'id': 'transfer', 'symbol': 'SOLUSD', 'activity_type': 'JNLS', 'qty': '1'})
    else:
        qty += 1
    before = await r.position_lots.list_all()
    with pytest.raises(AssetFeeIntegrityError, match='POPULATION'):
        await replay_asset_fees(r, ASSET, raw, qty, now=NOW)
    assert await r.position_lots.list_all() == before
    assert await r.reconciliation_records.list_all() == []


def cash_receipt(identifier='cash-1', amount='-3.54'):
    return {'id': identifier, 'activity_type': 'CFEE', 'created_at': (NOW+timedelta(days=1)).isoformat(),
            'currency': 'USD', 'date': '2026-09-18', 'description': 'Coin Pair Transaction Fee (USD)',
            'net_amount': amount, 'status': 'executed'}


async def test_usd_fees_are_recorded_once_and_allocations_conserve_actual_expense(tmp_path):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    r = await seeded(tmp_path)
    await sold_history(r)
    raw, qty = await population(r, four_receipts())
    raw += [cash_receipt(), cash_receipt('cash-2', '-5.77')]
    stamp = NOW+timedelta(days=2)
    assert await replay_asset_fees(r, ASSET, raw, qty, now=stamp)
    entries = [x['payload'] for x in await r.cash_ledger.list_all()]
    assert len(entries) == 2
    assert sum((D(x['amount']) for x in entries), D(0)) == D('-9.31')
    records = [x['payload'] for x in await r.reconciliation_records.list_all()]
    cash_records = [x for x in records if x['record_id'].startswith('asset_cash_fee:')]
    assert len(cash_records) == 2
    for row in cash_records:
        assert sum((D(v) for v in row['actual']['allocations'].values()), D(0)) == -D(row['actual']['activity']['net_amount'])
        assert row['actual']['order']['broker_order_ids'] == ['sell-order']
    for _ in range(3):
        assert not await replay_asset_fees(r, ASSET, raw, qty, now=stamp)
    assert len(await r.reconciliation_records.list_all()) == len(records)
    assert [x['payload'] for x in await r.cash_ledger.list_all()] == entries


async def test_unlinked_cash_fee_uses_exhaustive_single_asset_population_not_a_guessed_order(tmp_path):
    from tradepulse.reconciliation.fee_population import validate_fee_population
    r = await seeded(tmp_path)
    await sold_history(r)
    raw, qty = await population(r, four_receipts())
    fills = [hydrate('fills', row['payload']) for row in await r.fills.list_all()]
    intents = [hydrate('trade_intents', row['payload']) for row in await r.trade_intents.list_all()]
    sell = next(f for f in fills if f.side == Side.SELL)
    alternative = replace(sell, fill_id='alternative', broker_fill_id='alternative', order_id='another-order', trade_intent_id='another-intent')
    fills.append(alternative)
    intents.append(replace(next(i for i in intents if i.side == Side.SELL), trade_intent_id='another-intent', broker_order_id='another-order'))
    sale_raw = next(x for x in raw if x.get('id') == sell.broker_fill_id)
    raw += [{**sale_raw, 'id': 'alternative', 'order_id': 'another-order'}, cash_receipt()]
    _, proof = validate_fee_population(ASSET, raw, fills, intents, qty-alternative.quantity)
    linked = proof['cash_fee_populations']['cash-1']
    assert linked['broker_order_ids'] == ['another-order', 'sell-order']
    assert linked['trade_intent_ids'] == ['another-intent', 'sell-intent']
    assert linked['method'] == 'exhaustive_single_asset_sell_population'


async def test_cash_insert_failure_rolls_back_entire_historical_replay(tmp_path):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees
    r = await seeded(tmp_path)
    await sold_history(r)
    tables = [r.position_lots, r.trade_attributions, r.settlements, r.trade_intents,
              r.holdings, r.cash_ledger, r.reconciliation_records]
    before = [await table.list_all() for table in tables]
    await r.position_lots.database.run(lambda c: c.execute(
        "CREATE TRIGGER reject_second_fee BEFORE INSERT ON cash_ledger "
        "WHEN NEW.record_id='asset_cash_fee:cash-2' BEGIN SELECT RAISE(ABORT,'cash rollback'); END"), write=True)
    raw, qty = await population(r, four_receipts())
    raw += [cash_receipt(), cash_receipt('cash-2', '-5.77')]
    with pytest.raises(DatabaseError, match='cash rollback'):
        await replay_asset_fees(r, ASSET, raw, qty, now=NOW+timedelta(days=2))
    assert [await table.list_all() for table in tables] == before
