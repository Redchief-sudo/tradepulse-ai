"""Synthetic regression inputs, never the immutable evidence or broker account."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from tradepulse.broker.types import AlpacaAccount, AlpacaPosition
from tradepulse.models import AssetClass, AssetIdentity, Holding, PositionLot, asset_identity_key
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.reconciliation.coordinator import _reconcile_positions
from tradepulse.risk import load_session
from tradepulse.valuation import marked_snapshot, record_valuation

NOW = datetime(2026, 9, 17, 23, 31, tzinfo=UTC)


async def repos(tmp_path):
    db = AsyncSQLiteDatabase(f'sqlite:///{tmp_path}/test.db')
    await db.initialize()
    return PersistenceRepositories.create(db)


def account(value='20.246913578'):
    return AlpacaAccount(D(value)+100, D(100), D(100), D(100), D(value)+100,
                         {'long_market_value': D(value), 'short_market_value': D(0), 'accrued_fees': D('0.23')})


def position(symbol='AAPL', cls=AssetClass.EQUITY, qty='2.123456789'):
    return AlpacaPosition(symbol, cls, D(qty), D(5), D('20.246913578'), D(10), D('9.629629633'), D('10.617283945'))


async def test_marked_reporting_precision_cost_and_price_change(tmp_path):
    r = await repos(tmp_path)
    asset = AssetIdentity('AAPL', AssetClass.EQUITY, 'alpaca:AAPL')
    await r.holdings.create_once(asset_identity_key(asset), Holding(asset, D('2.123456789'), D(5), NOW, sector='Tech'))
    p = position()
    s = await marked_snapshot(r, account(), [p], now=NOW)
    assert s.holdings_value == D('20.246913578')
    assert s.holdings_cost_basis == D('10.617283945')
    assert s.sector_exposure == {'Tech': s.holdings_value}
    assert s.sector_cost_basis == {'Tech': s.holdings_cost_basis}
    assert s.equity_reconciliation_status == 'matched'
    assert s.broker_equity_components['accrued_fees'] == D('.23')
    changed = await marked_snapshot(r, account('22'), [replace(p, market_value=D(22))], now=NOW)
    assert changed.holdings_value == 22
    assert changed.holdings_cost_basis == s.holdings_cost_basis
    await r.equity_snapshots.create_once(s.snapshot_id, s)
    restored = hydrate('equity_snapshots', (await r.equity_snapshots.get(s.snapshot_id))['payload'])
    assert restored == s


@pytest.mark.parametrize('problem', ['equity', 'missing_components'])
async def test_valuation_discrepancies_persist_failure_without_balancing(tmp_path, problem):
    r = await repos(tmp_path)
    a, p = account(), position()
    if problem == 'equity':
        a = replace(a, equity=a.equity+D('.000000001'))
    elif problem == 'position':
        p = replace(p, market_value=p.market_value+D('.000000001'))
    elif problem == 'missing_components':
        a = replace(a, equity_components={})
    else:
        p = replace(p, cost_basis=None)
    s = await marked_snapshot(r, a, [p], now=NOW)
    await record_valuation(r, s)
    await record_valuation(r, s)
    assert s.equity_reconciliation_status == 'failed'
    assert s.holdings_value == p.market_value
    assert s.total_equity == a.equity
    rows = await r.reconciliation_records.list_all()
    assert len(rows) == 1
    assert rows[0]['payload']['outcome'] == 'unresolved_mismatch'


async def test_short_and_explicit_memopost_components(tmp_path):
    r = await repos(tmp_path)
    a = AlpacaAccount(D(75), D(100), D(100), D(100), D(75),
                      {'long_market_value': D(0), 'short_market_value': D(-20), 'memoposts': D(5)})
    p = replace(position(), qty=D(-2), market_value=D(-20), cost_basis=D(-10))
    s = await marked_snapshot(r, a, [p], now=NOW)
    assert s.holdings_value == -20
    assert s.equity_reconciliation_difference == 0
    assert s.equity_reconciliation_status == 'matched'


@pytest.mark.parametrize('broker_qty', ['36.72363133', '36.631822251'])
async def test_sol_fractional_quantity_detection_is_persisted_and_never_mutates_lots(tmp_path, broker_qty):
    r = await repos(tmp_path)
    asset = AssetIdentity('SOL/USD', AssetClass.CRYPTO, 'alpaca:SOL/USD')
    key = asset_identity_key(asset)
    qty = D('36.72363133')
    for i, q in enumerate(['13.9083', '2.75634854', '14.0172', '6.04178279']):
        lot = PositionLot(str(i), str(i), asset, 'long', D(q), D(q), D(105), NOW)
        await r.position_lots.create_once(str(i), lot, unique_value=str(i))
    await r.holdings.create_once(key, Holding(asset, qty, D(105), NOW))
    before = await r.position_lots.list_all()
    broker = AsyncMock()
    broker.get_positions.return_value = [position('SOL/USD', AssetClass.CRYPTO, broker_qty)]
    alerts = AsyncMock()
    for _ in range(2):
        result = await _reconcile_positions(r, broker, alerts, NOW)
        assert result == (1, 0, int(broker_qty != str(qty)))
    assert await r.position_lots.list_all() == before
    assert (await r.holdings.get(key))['payload']['quantity'] == str(qty)
    records = await r.reconciliation_records.list_all()
    assert all(row['payload']['subject_id'] == key for row in records if row['payload']['reconciliation_type'].startswith('position'))
    if broker_qty != str(qty):
        assert qty-D(broker_qty) == D('0.091809079')
        assert all(row['payload']['outcome'] == 'drift_detected' for row in records)
        assert not (await load_session(r)).financial_integrity_manual_reenable_required
        assert (await r.accounting_epochs.list_all())[0]['status'] == 'integrity_blocked'


async def test_duplicate_canonical_broker_identity_is_rejected(tmp_path):
    r = await repos(tmp_path)
    with pytest.raises(ValueError, match='duplicate broker position identity'):
        await marked_snapshot(r, account(), [position(), position()], now=NOW)


async def test_bare_symbol_cannot_merge_distinct_asset_classes(tmp_path):
    r = await repos(tmp_path)
    equity = AssetIdentity('SOL', AssetClass.EQUITY, 'alpaca:SOL')
    await r.holdings.create_once(asset_identity_key(equity), Holding(equity, D(1), D(10), NOW))
    lot = PositionLot('l', 'f', equity, 'long', D(1), D(1), D(10), NOW)
    await r.position_lots.create_once('l', lot, unique_value='f')
    broker = AsyncMock()
    broker.get_positions.return_value = [position('SOL', AssetClass.CRYPTO, '1')]
    assert await _reconcile_positions(r, broker, AsyncMock(), NOW) == (2, 0, 2)
    records = await r.reconciliation_records.list_all()
    assert len({row['payload']['subject_id'] for row in records if row['payload']['reconciliation_type'].startswith('position')}) == 2


def test_unreconciled_position_prevents_prove_edge_success():
    from python_tests.test_paper_verification import COSTS, START, passing_rows
    from python_tests.test_paper_verification import NOW as ASSESS_NOW
    from tradepulse.verification.evidence import assess

    rows = passing_rows()
    rows['reconciliation_records'][0]['outcome'] = 'drift_detected'
    result = assess(rows, START.isoformat(), ASSESS_NOW, COSTS)
    assert result['status'] == 'PROVE_EDGE_FAILED_INTEGRITY'
    assert result['criteria']['eligible_round_trips']['actual'] == 0
    assert result['population'] == []
    assert not result['criteria']['reconciliation_issues']['passed']


@pytest.mark.parametrize('defect', ['equity_only', 'bare_symbol', 'stale'])
def test_prove_edge_requires_current_canonical_position_receipt(defect):
    from python_tests.test_paper_verification import COSTS, START, passing_rows
    from python_tests.test_paper_verification import NOW as ASSESS_NOW
    from tradepulse.verification.evidence import assess

    rows = passing_rows()
    receipt = rows['reconciliation_records'][0]
    if defect == 'equity_only':
        receipt['reconciliation_type'] = 'equity'
    elif defect == 'bare_symbol':
        receipt['subject_id'] = 'X'
    else:
        receipt['occurred_at'] = START.isoformat()
    result = assess(rows, START.isoformat(), ASSESS_NOW, COSTS)
    assert result['status'] == 'PROVE_EDGE_FAILED_INTEGRITY'
    assert result['criteria']['eligible_round_trips']['actual'] == 0
    assert result['population'] == []
    assert not result['criteria']['evidence_errors']['passed']


@pytest.mark.parametrize('difference', ['0.000000001', '0.009', '2.52', '10000'])
async def test_separate_position_observations_do_not_fail_equity_arithmetic(tmp_path, difference):
    r = await repos(tmp_path)
    a, p = account(), position()
    s = await marked_snapshot(r, a, [replace(p, market_value=p.market_value+D(difference))], now=NOW)
    assert s.equity_reconciliation_status == 'matched'
    assert s.equity_reconciliation_difference == 0
    assert s.position_value_observation_difference == D(difference)
    assert s.position_value_observation_status == 'different_uncoordinated_observations'
    await record_valuation(r, s)
    row = (await r.reconciliation_records.list_all())[0]['payload']
    assert D(row['actual']['position_value_observation_difference']) == D(difference)
    await r.equity_snapshots.create_once(s.snapshot_id, s)
    assert hydrate('equity_snapshots', (await r.equity_snapshots.get(s.snapshot_id))['payload']) == s


async def test_observation_times_are_transport_times_not_a_common_price_instant(tmp_path):
    r = await repos(tmp_path)
    a = replace(account(), received_at=NOW)
    p = replace(position(), received_at=NOW+timedelta(seconds=1))
    s = await marked_snapshot(r, a, [p], now=NOW+timedelta(seconds=2))
    assert s.position_value_observation_status == 'equal_uncoordinated_observations'
    assert s.valuation_observation_times['account_response_received_at'] == NOW.isoformat()
    assert s.valuation_observation_times['position_response_received_at:equity:default:alpaca:AAPL'] == p.received_at.isoformat()
    await r.equity_snapshots.create_once(s.snapshot_id, s)
    assert hydrate('equity_snapshots', (await r.equity_snapshots.get(s.snapshot_id))['payload']) == s


async def test_missing_cost_basis_does_not_change_exact_equity_arithmetic(tmp_path):
    r = await repos(tmp_path)
    s = await marked_snapshot(r, account(), [replace(position(), cost_basis=None)], now=NOW)
    assert s.equity_reconciliation_status == 'matched'
    assert s.equity_reconciliation_difference == 0
    assert s.holdings_cost_basis is None
    assert any(error.startswith('cost_basis_missing:') for error in s.valuation_errors)
