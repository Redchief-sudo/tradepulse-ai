from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from test_settlement_engine import _repositories, _seed_buy, _no_op_alerter, NOW, asset
from tradepulse.models import Fill, SettlementEvent, TradeIntent, Side, ExecutionMode, TradeIntentStatus
from tradepulse.persistence import hydrate
from tradepulse.settlement import SettlementProcessor
from tradepulse.settlement.accounting import replay_accounting, ProjectionEvidenceError


async def two_fill_exit(repositories):
    await _seed_buy(repositories, quantity='50.303', price='317.1041')
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    assert (await processor.process_pending()).completed == 1
    intent = TradeIntent('exit', 'exit-key', 'corr-exit', asset(), Side.SELL, ExecutionMode.PAPER, 'manual', NOW,
                         requested_quantity=Decimal('50.303'))
    await repositories.trade_intents.create_once('exit', intent, status=intent.status.value, unique_value='exit-key')
    for i, qty in enumerate(('50', '.303')):
        fid = f'exit-{i}'
        fill = Fill(fid, 'exit', 'sell-order', asset(), Side.SELL, ExecutionMode.PAPER,
                    Decimal(qty), Decimal('339.01'), Decimal(0), Decimal(0), NOW)
        await repositories.fills.create_once(fid, fill)
        event = SettlementEvent(fid, fid, 'exit', asset(), Side.SELL, ExecutionMode.PAPER,
                                fill.quantity, fill.price, NOW)
        await repositories.settlements.create_once(fid, event, status=event.status.value, unique_value=fid)
    return processor


async def test_two_fill_cash_pnl_and_replay(tmp_path):
    r = await _repositories(tmp_path)
    p = await two_fill_exit(r)
    assert (await p.process_pending()).completed == 2
    cash = await r.cash_ledger.list_all()
    pnl = await r.pnl_records.list_all()
    expected = Decimal('50.303') * (Decimal('339.01') - Decimal('317.1041'))
    assert len(cash) == 3
    assert len(pnl) == 2
    assert sum((Decimal(row['payload']['amount']) for row in cash), Decimal(0)) == expected
    assert sum((Decimal(row['payload']['realized']) for row in pnl), Decimal(0)) == expected
    assert await r.holdings.list_all() == []
    assert await replay_accounting(r) == 0
    assert (await p.process_pending()).processed == 0
    # Reproduce legacy completed settlements with missing canonical destinations.
    await r.cash_ledger.database.run(lambda c: (c.execute('DELETE FROM cash_ledger'), c.execute('DELETE FROM pnl_records')), write=True)
    assert await replay_accounting(r) == 5
    assert await replay_accounting(r) == 0
    assert sorted(await r.cash_ledger.list_all(), key=lambda row: row['record_id']) == sorted(cash, key=lambda row: row['record_id'])
    assert sorted(await r.pnl_records.list_all(), key=lambda row: row['record_id']) == sorted(pnl, key=lambda row: row['record_id'])


async def test_pnl_write_failure_is_retryable_and_no_duplicate_cash(tmp_path):
    r = await _repositories(tmp_path)
    p = await two_fill_exit(r)
    await r.pnl_records.database.run(lambda c: c.execute(
        "CREATE TRIGGER fail_pnl BEFORE INSERT ON pnl_records BEGIN SELECT RAISE(ABORT,'injected disk failure'); END"), write=True)
    result = await p.process_pending()
    assert result.completed == 0
    for row in await r.settlements.list_by_status('retryable_failed'):
        assert not row['payload']['trade_projected']
    await r.pnl_records.database.run(lambda c: c.execute('DROP TRIGGER fail_pnl'), write=True)
    assert (await p.process_pending(force_retry=True)).completed == 2
    assert len(await r.cash_ledger.list_all()) == 3
    assert len(await r.pnl_records.list_all()) == 2


async def test_checkpoint_refuses_lying_handler_and_replay_rejects_conflict(tmp_path):
    r = await _repositories(tmp_path)
    await _seed_buy(r)
    p = SettlementProcessor(r, _no_op_alerter(), clock=lambda: NOW)
    event = hydrate('settlements', (await r.settlements.get('se-1'))['payload'])
    with pytest.raises(ProjectionEvidenceError, match='DURABLE_ROW_MISSING'):
        await p._checkpoint(replace(event, cash_projected=True))
    assert not (await r.settlements.get('se-1'))['payload']['cash_projected']
    assert (await p.process_pending()).completed == 1
    row = (await r.cash_ledger.list_all())[0]
    bad = dict(row['payload'], amount='123')
    from tradepulse.persistence.codec import encode_payload
    await r.cash_ledger.database.run(lambda c: c.execute('UPDATE cash_ledger SET payload=? WHERE record_id=?',
        (encode_payload(bad), row['record_id'])), write=True)
    with pytest.raises(ProjectionEvidenceError, match='DURABLE_ROW_CONFLICT'):
        await replay_accounting(r)


async def test_zero_equity_difference_cannot_mask_position_mismatch(tmp_path):
    from test_forensic_corrections import account, position
    from tradepulse.valuation import marked_snapshot, record_valuation
    from tradepulse.risk import load_session
    from tradepulse.models import SessionState
    r = await _repositories(tmp_path)
    snapshot = await marked_snapshot(r, account(), [position()], now=NOW)
    assert snapshot.equity_reconciliation_difference == 0
    await record_valuation(r, snapshot)
    row = (await r.reconciliation_records.list_all())[0]['payload']
    assert row['outcome'] == 'unresolved_mismatch'
    assert (await load_session(r)).state == SessionState.FINANCIAL_INTEGRITY_BLOCKED


def test_missing_canonical_journals_and_hold_prevent_prove_edge_count():
    from test_paper_verification import passing_rows, START, NOW, COSTS
    from tradepulse.verification.evidence import assess
    for defect in ('cash_ledger', 'pnl_records', 'integrity_holds'):
        rows = passing_rows()
        if defect == 'integrity_holds':
            rows[defect].append({'hold_type': 'verification_pending'})
        else:
            rows[defect] = []
        result = assess(rows, START.isoformat(), NOW, COSTS)
        assert result['criteria']['eligible_round_trips']['actual'] == 0
        assert result['status'] != 'PROVE_EDGE_PASSED'


async def test_legacy_attribution_contract_migration_requires_durable_evidence(tmp_path):
    from tradepulse.persistence.codec import encode_payload
    r = await _repositories(tmp_path)
    await _seed_buy(r)
    p = SettlementProcessor(r, _no_op_alerter(), clock=lambda: NOW)
    assert (await p.process_pending()).completed == 1
    original = dict((await r.settlements.get('se-1'))['payload'])
    original.pop('attribution_projected')
    await r.settlements.database.run(lambda c: (
        c.execute('UPDATE settlements SET payload=? WHERE record_id=?', (encode_payload(original), 'se-1')),
        c.execute('DELETE FROM cash_ledger')), write=True)
    assert await replay_accounting(r) == 1
    assert (await r.settlements.get('se-1'))['payload']['attribution_projected'] is True
    migration = next(row['payload'] for row in await r.reconciliation_records.list_all()
                     if row['record_id'].startswith('accounting_stage_migration:'))
    assert migration['actual']['before'] == original
    assert len(await r.cash_ledger.list_all()) == 1
    assert await replay_accounting(r) == 0


async def test_fractional_quantities_and_full_identity_never_collide(tmp_path):
    from tradepulse.models import AssetIdentity, AssetClass, asset_identity_key
    r = await _repositories(tmp_path)
    instruments = [AssetIdentity('SAME', AssetClass.EQUITY, 'native-one', venue='venue-one'),
                   AssetIdentity('SAME', AssetClass.EQUITY, 'native-two', venue='venue-two'),
                   AssetIdentity('SOL/USD', AssetClass.CRYPTO, 'alpaca:SOL/USD')]
    for i, instrument in enumerate(instruments):
        identifier = str(i)
        intent = TradeIntent(identifier, identifier, identifier, instrument, Side.BUY, ExecutionMode.PAPER, 'test', NOW,
                             requested_quantity=Decimal('0.091809079'))
        await r.trade_intents.create_once(identifier, intent, status=intent.status.value, unique_value=identifier)
        fill = Fill(identifier, identifier, identifier, instrument, Side.BUY, ExecutionMode.PAPER,
                    Decimal('0.091809079'), Decimal('105.103'), Decimal(0), Decimal(0), NOW)
        await r.fills.create_once(identifier, fill)
        event = SettlementEvent(identifier, identifier, identifier, instrument, Side.BUY, ExecutionMode.PAPER,
                                fill.quantity, fill.price, NOW)
        await r.settlements.create_once(identifier, event, status=event.status.value, unique_value=identifier)
    processor = SettlementProcessor(r, _no_op_alerter(), clock=lambda: NOW)
    assert (await processor.process_pending()).completed == 3
    holdings = await r.holdings.list_all()
    assert {row['record_id'] for row in holdings} == {asset_identity_key(a) for a in instruments}
    assert all(Decimal(row['payload']['quantity']) == Decimal('0.091809079') for row in holdings)
    assert len(await r.position_lots.list_all()) == 3
    assert len(await r.cash_ledger.list_all()) == 3
    assert await replay_accounting(r) == 0


async def test_failed_new_population_supersedes_verified_equity_checkpoint(tmp_path):
    from types import SimpleNamespace
    from tradepulse.models import TradeIntentStatus
    from tradepulse.reconciliation.equity_epochs import reconcile_equity_epochs
    from test_accounting_epochs import pagination
    r = await _repositories(tmp_path)
    intent = TradeIntent('entry', 'entry', 'opportunity', asset(), Side.BUY, ExecutionMode.PAPER, 'test', NOW,
        requested_quantity=Decimal(1), status=TradeIntentStatus.FILLED, broker_order_id='order')
    await r.trade_intents.create_once('entry', intent, status='filled', unique_value='entry')
    fill = Fill('receipt', 'entry', 'order', asset(), Side.BUY, ExecutionMode.PAPER,
                Decimal(1), Decimal(100), Decimal(0), Decimal(0), NOW, broker_fill_id='receipt')
    await r.fills.create_once('receipt', fill, unique_value='receipt')
    event = SettlementEvent('receipt', 'receipt', 'entry', asset(), Side.BUY, ExecutionMode.PAPER,
        fill.quantity, fill.price, NOW, broker_order_id='order', broker_fill_id='receipt')
    await r.settlements.create_once('receipt', event, status='pending', unique_value='receipt')
    assert (await SettlementProcessor(r, _no_op_alerter(), clock=lambda: NOW).process_pending()).completed == 1
    raw = {'id': 'receipt', 'activity_type': 'FILL', 'symbol': 'AAPL', 'side': 'buy', 'qty': '1',
           'price': '100', 'order_id': 'order', 'transaction_time': NOW.isoformat()}
    class Broker:
        failed = False
        async def get_activities(self, *, activity_type, page_evidence):
            if self.failed:
                raise ValueError('new population unavailable')
            page_evidence.extend(pagination([raw])['pages'])
            return [SimpleNamespace(raw=raw, activity_id='receipt')]
        async def get_positions(self):
            return [SimpleNamespace(asset_class=asset().asset_class, symbol='AAPL', qty=Decimal(1))]
    broker = Broker()
    assert await reconcile_equity_epochs(r, broker, now=NOW)
    old = (await r.accounting_epochs.list_all())[0]['payload']
    receipt = await r.reconciliation_records.get(old['checkpoint_id'])
    broker.failed = True
    with pytest.raises(ValueError, match='new population unavailable'):
        await reconcile_equity_epochs(r, broker, now=NOW)
    new = (await r.accounting_epochs.list_all())[0]['payload']
    assert new['fee_accounting_status'] == 'fee_pending'
    assert old['checkpoint_id'] in new['superseded_checkpoint_ids']
    assert await r.reconciliation_records.get(old['checkpoint_id']) == receipt
    before = await r.reconciliation_records.list_all()
    with pytest.raises(ValueError, match='new population unavailable'):
        await reconcile_equity_epochs(r, broker, now=NOW)
    assert await r.reconciliation_records.list_all() == before


async def test_historical_fee_in_full_feed_does_not_block_new_equity_epoch(tmp_path):
    from types import SimpleNamespace
    from tradepulse.reconciliation.equity_epochs import reconcile_equity_epochs
    from test_accounting_epochs import pagination

    r = await _repositories(tmp_path)
    intent = TradeIntent('entry', 'entry', 'opportunity', asset(), Side.BUY, ExecutionMode.PAPER, 'test', NOW,
        requested_quantity=Decimal(1), status=TradeIntentStatus.FILLED, broker_order_id='order')
    await r.trade_intents.create_once('entry', intent, status='filled', unique_value='entry')
    fill = Fill('receipt', 'entry', 'order', asset(), Side.BUY, ExecutionMode.PAPER,
                Decimal(1), Decimal(100), Decimal(0), Decimal(0), NOW, broker_fill_id='receipt')
    await r.fills.create_once('receipt', fill, unique_value='receipt')
    event = SettlementEvent('receipt', 'receipt', 'entry', asset(), Side.BUY, ExecutionMode.PAPER,
        fill.quantity, fill.price, NOW, broker_order_id='order', broker_fill_id='receipt')
    await r.settlements.create_once('receipt', event, status='pending', unique_value='receipt')
    assert (await SettlementProcessor(r, _no_op_alerter(), clock=lambda: NOW).process_pending()).completed == 1

    historical_fee = {
        'id': 'fee-old', 'activity_type': 'FEE', 'currency': 'USD', 'status': 'executed',
        'net_amount': '-0.50', 'created_at': (NOW - timedelta(days=2)).isoformat(), 'date': '2025-01-01',
    }
    fill_raw = {'id': 'receipt', 'activity_type': 'FILL', 'symbol': 'AAPL', 'side': 'buy', 'qty': '1',
                'price': '100', 'order_id': 'order', 'transaction_time': NOW.isoformat()}

    class Broker:
        async def get_activities(self, *, activity_type, page_evidence):
            page_evidence.extend(pagination([historical_fee, fill_raw])['pages'])
            return [SimpleNamespace(raw=historical_fee, activity_id='fee-old'),
                    SimpleNamespace(raw=fill_raw, activity_id='receipt')]

        async def get_positions(self):
            return [SimpleNamespace(asset_class=asset().asset_class, symbol='AAPL', qty=Decimal(1))]

    assert await reconcile_equity_epochs(r, Broker(), now=NOW)
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert epoch['fee_accounting_status'] == 'reconciled_net'
    assert epoch['population_proof_id'] is not None
    assert 'generation_opened_at' not in epoch  # unbound operational evidence has no official boundary
    assert 'generation_boundary' not in epoch


async def test_generation_boundary_uses_frozen_epoch_timestamp_not_fill_opened_time(tmp_path):
    from tradepulse.reconciliation.epochs import _generation_boundary

    epoch = {
        'opened_at': (NOW - timedelta(days=3)).isoformat(),
        'generation_opened_at': NOW.isoformat(),
    }
    boundary = _generation_boundary(epoch)
    assert boundary == NOW.astimezone(UTC)
    assert boundary != (NOW - timedelta(days=3)).astimezone(UTC)


def test_explicit_fill_fees_reduce_net_once_without_changing_gross():
    from test_paper_verification import passing_rows, START, NOW, COSTS
    from tradepulse.verification.evidence import assess
    rows = passing_rows(count=1, wins=1)
    for fill, fee in zip(rows['fills'], ('0.01', '0.02')):
        fill['fees'] = fee
        cash = next(row for row in rows['cash_ledger'] if row['entry_id'] == 'fill:cash:' + fill['fill_id'])
        cash['amount'] = str(Decimal(cash['amount']) - Decimal(fee))
        rows['pnl_records'].append({'record_id': 'fill:fee:' + fill['fill_id'], 'asset': fill['asset'],
            'realized': str(-Decimal(fee)), 'unrealized': '0', 'as_of': fill['filled_at']})
    result = assess(rows, START.isoformat(), NOW, COSTS)
    assert result['criteria']['eligible_round_trips']['actual'] == 1
    assert Decimal(result['criteria']['net_realized_pnl']['actual']) == Decimal('1.9596')
    assert result['modeled_trade_net'] != result['observed_generation_net']
    assert rows['trade_attributions'][0]['realized_pnl'] == '2'
