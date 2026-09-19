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
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, DatabaseError, PersistenceRepositories, hydrate
from tradepulse.reconciliation.asset_fees import (
    AssetFeeIntegrityError,
    AssetFeePending,
    apply_asset_fee,
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
                            holding_projected=True, trade_projected=True, integrity_verified=True)
        await r.settlements.create_once(fid, e, status='completed', unique_value=fid)
        lot = PositionLot(f'lot-{i}', fid, ASSET, 'long', D(q), D(q), D(p), stamp)
        await r.position_lots.create_once(lot.lot_id, lot, unique_value=fid)
        total += D(q)
        cost += D(q)*D(p)
    h = Holding(ASSET, total, cost/total, NOW, sector='Crypto', stop_loss=D(94), current_stop=D('101.123456789'))
    await r.holdings.create_once(KEY, h)
    return r


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
    assert len(await r.reconciliation_records.list_all()) == 4
    assert await r.fills.list_all() == original_fills
    assert await r.settlements.list_all() == original_settlements


async def test_concurrent_replay_debits_once(tmp_path):
    r = await seeded(tmp_path)
    fee = parse_asset_fee(receipt())
    results = await asyncio.gather(apply_asset_fee(r, fee, now=NOW), apply_asset_fee(r, fee, now=NOW))
    assert sorted(results) == [False, True]
    assert len(await r.reconciliation_records.list_all()) == 1
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


async def test_late_fee_after_a_sale_requires_historical_replay(tmp_path):
    r = await seeded(tmp_path)
    fill = Fill('sell', 'sell-intent', 'sell-order', ASSET, Side.SELL, ExecutionMode.PAPER, D(1), D(110), D(0), D(0), NOW, 'sell')
    await r.fills.create_once('sell', fill, unique_value='sell')
    lot = hydrate('position_lots', (await r.position_lots.get('lot-0'))['payload'])
    await r.position_lots.update('lot-0', replace(lot, remaining_quantity=lot.remaining_quantity-1, closures={'sell': D(1)}))
    before = (await r.holdings.get(KEY), await r.position_lots.list_all())
    with pytest.raises(AssetFeeIntegrityError, match='HISTORICAL_REPLAY_REQUIRED'):
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
    broker = AsyncMock()
    broker.get_activities.return_value = [SimpleNamespace(raw=raw) for raw in reversed(four_receipts())]
    assert await reconcile_asset_fees(r, broker, now=NOW)
    assert D((await r.holdings.get(KEY))['payload']['quantity']) == D('36.631822251')
    assert broker.get_activities.call_args.kwargs['since'] == FIRST.replace(hour=0, minute=0, second=0)


async def test_reconciliation_failure_is_persisted_and_latched(tmp_path):
    r = await seeded(tmp_path)
    broker = AsyncMock()
    broker.get_activities.return_value = [SimpleNamespace(raw=receipt(quantity='-100'))]
    before = await r.position_lots.list_all()
    assert not await reconcile_asset_fees(r, broker, now=NOW)
    assert await r.position_lots.list_all() == before
    assert (await load_session(r)).financial_integrity_manual_reenable_required
    records = await r.reconciliation_records.list_all()
    assert records[0]['payload']['outcome'] == 'drift_detected'


@pytest.mark.parametrize('tamper', [None, 'missing', 'amount', 'asset'])
def test_prove_edge_conserves_fee_units_and_expenses_basis_once(tamper):
    from python_tests.test_paper_verification import COSTS, START, passing_rows
    from python_tests.test_paper_verification import NOW as ASSESS_NOW
    from tradepulse.persistence.codec import decode_payload, encode_payload
    from tradepulse.verification.evidence import assess

    rows = passing_rows(count=1, wins=1)
    asset = decode_payload(encode_payload(ASSET))
    for table in ['fills', 'settlements', 'position_lots', 'trade_attributions']:
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
    if tamper == 'amount':
        record['actual']['cost_basis_debit'] = '0'
    if tamper == 'asset':
        record['actual']['activity']['symbol'] = 'BTCUSD'
        record['actual']['asset_key'] = 'crypto:default:alpaca:BTC/USD'
    if tamper != 'missing':
        rows['reconciliation_records'].append(record)
    result = assess(rows, START.isoformat(), ASSESS_NOW, COSTS)
    if tamper is None:
        assert result['criteria']['eligible_round_trips']['actual'] == 1
        assert D(result['criteria']['net_realized_pnl']['actual']) == D('-8.23836')  # -8.2 actual result, less the unchanged 2 bps model on 191.8 notional
        assert result['criteria']['evidence_errors']['actual'] == 0
    else:
        assert result['status'] == 'PROVE_EDGE_FAILED_INTEGRITY'
        assert result['population'] == []
