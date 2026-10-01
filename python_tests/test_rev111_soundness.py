"""Rev.111 soundness corrections found by the 2026-09-29 accounting soak.

1. Re-reading an unchanged broker population produced new per-page
   ``received_at`` transport times, so every poll minted a new membership
   record, a new population proof and a superseded checkpoint (368 false
   ``drift_detected`` supersessions and ~155 MB in 10 hours for one option).
2. Generation equity evidence was bounded by runtime-segment times, but the
   guarded startup reconciliation snapshot precedes the segment start and the
   runner's mandatory post-shutdown reconciliation follows its end.
3. An option identity without a contract multiplier silently valued at 1x.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_accounting_epochs import ASSET, NOW, four_receipts, pagination, population, seeded
from test_generation_membership import OPENED, boundary, receipt  # noqa: F401 - pytest fixture

from tradepulse.persistence.codec import decode_payload
from tradepulse.reconciliation.membership import classify_population, verify_membership_record


def received(raw, at):
    proof = pagination(raw)
    for page in proof['pages']:
        page['received_at'] = at.isoformat()
    return proof


def test_identical_population_observations_share_one_membership_record(boundary):  # noqa: F811
    connection, checkpoint = boundary
    raw = [*checkpoint['activities'], receipt('in-generation', OPENED + timedelta(minutes=1))]
    first = classify_population(connection, raw, received(raw, OPENED + timedelta(hours=1)), now=OPENED + timedelta(hours=1))
    second = classify_population(connection, raw, received(raw, OPENED + timedelta(hours=2)), now=OPENED + timedelta(hours=2))
    assert first['record_id'] == second['record_id']
    rows = connection.execute("SELECT payload FROM reconciliation_records").fetchall()
    assert len(rows) == 1
    record = decode_payload(rows[0][0])
    assert all('received_at' not in page for page in record['actual']['pagination']['pages'])
    assert verify_membership_record(checkpoint, record) == first['classifications']


def test_changed_population_still_mints_a_new_membership_record(boundary):  # noqa: F811
    connection, checkpoint = boundary
    raw = [*checkpoint['activities'], receipt('in-generation', OPENED + timedelta(minutes=1))]
    first = classify_population(connection, raw, received(raw, OPENED + timedelta(hours=1)), now=OPENED + timedelta(hours=1))
    raw = [*raw, receipt('late-fee', OPENED + timedelta(minutes=2))]
    second = classify_population(connection, raw, received(raw, OPENED + timedelta(hours=2)), now=OPENED + timedelta(hours=2))
    assert first['record_id'] != second['record_id']
    assert connection.execute("SELECT count(*) FROM reconciliation_records").fetchone()[0] == 2


async def _bound_equity_position(tmp_path, monkeypatch):
    from test_settlement_engine import _no_op_alerter, _repositories, asset

    from tradepulse.models import ExecutionMode, Fill, SettlementEvent, Side, TradeIntent, TradeIntentStatus
    from tradepulse.settlement import SettlementProcessor

    repositories = await _repositories(tmp_path)
    opening_history = [receipt('old-fee', OPENED - timedelta(days=2))]
    checkpoint = {'verification_generation_id': 'soak', 'checkpoint_id': 'opening', 'opened_at': OPENED.isoformat(),
                  'activities': opening_history, 'positions': [],
                  'opening_activity_cursor': {'kind': 'broker_activity', 'last_activity_id': 'old-fee'},
                  'opening_activity_population_hash': 'opening-hash', 'account_identity_digest': 'account-hash'}
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_opening_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_closing_checkpoint', lambda _: None)
    stamp = OPENED + timedelta(minutes=1)
    intent = TradeIntent('entry', 'entry', 'opportunity', asset(), Side.BUY, ExecutionMode.PAPER, 'test', stamp,
                         requested_quantity=Decimal(1), status=TradeIntentStatus.FILLED, broker_order_id='order')
    await repositories.trade_intents.create_once('entry', intent, status='filled', unique_value='entry')
    fill = Fill('fill', 'entry', 'order', asset(), Side.BUY, ExecutionMode.PAPER,
                Decimal(1), Decimal(100), Decimal(0), Decimal(0), stamp, broker_fill_id='fill')
    await repositories.fills.create_once('fill', fill, unique_value='fill')
    event = SettlementEvent('fill', 'fill', 'entry', asset(), Side.BUY, ExecutionMode.PAPER,
                            fill.quantity, fill.price, stamp, broker_order_id='order', broker_fill_id='fill')
    await repositories.settlements.create_once('fill', event, status='pending', unique_value='fill')
    assert (await SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: stamp).process_pending()).completed == 1
    raw = [*opening_history, {'id': 'fill', 'activity_type': 'FILL', 'symbol': 'AAPL', 'side': 'buy', 'qty': '1',
                              'price': '100', 'order_id': 'order', 'transaction_time': stamp.isoformat()}]

    class Broker:
        """Like Alpaca: every response carries a fresh transport receipt time."""
        polls = 0

        async def get_activities(self, *, activity_type, page_evidence):
            Broker.polls += 1
            page_evidence.extend(received(raw, stamp + timedelta(seconds=Broker.polls))['pages'])
            return [SimpleNamespace(raw=r, activity_id=r['id']) for r in raw]

        async def get_positions(self):
            return [SimpleNamespace(asset_class=asset().asset_class, symbol='AAPL', qty=Decimal(1))]

    return repositories, Broker(), raw, stamp


def _supersessions(rows):
    return [row for row in rows if row['record_id'].startswith('epoch_proof_superseded:')]


async def test_transport_only_refresh_never_supersedes_equity_checkpoint(tmp_path, monkeypatch):
    from tradepulse.reconciliation.equity_epochs import reconcile_equity_epochs

    repositories, broker, raw, stamp = await _bound_equity_position(tmp_path, monkeypatch)
    assert await reconcile_equity_epochs(repositories, broker, now=OPENED, clock=lambda: stamp)
    initial = (await repositories.accounting_epochs.list_all())[0]['payload']
    records = await repositories.reconciliation_records.list_all()
    for minute in (2, 3, 4):
        assert await reconcile_equity_epochs(repositories, broker, now=stamp + timedelta(minutes=minute),
                                             clock=lambda m=minute: stamp + timedelta(minutes=m))
    assert (await repositories.accounting_epochs.list_all())[0]['payload'] == initial
    assert await repositories.reconciliation_records.list_all() == records
    assert _supersessions(records) == []

    # A genuinely new receipt still supersedes exactly once, then is stable.
    raw.append(receipt('late-account-fee', stamp + timedelta(minutes=5)))
    assert await reconcile_equity_epochs(repositories, broker, now=stamp + timedelta(minutes=6),
                                         clock=lambda: stamp + timedelta(minutes=6))
    after = (await repositories.accounting_epochs.list_all())[0]['payload']
    assert after['checkpoint_version'] == initial['checkpoint_version'] + 1
    assert after['superseded_checkpoint_ids'] == [initial['checkpoint_id']]
    settled = await repositories.reconciliation_records.list_all()
    assert len(_supersessions(settled)) == 1
    assert await reconcile_equity_epochs(repositories, broker, now=stamp + timedelta(minutes=7),
                                         clock=lambda: stamp + timedelta(minutes=7))
    assert await repositories.reconciliation_records.list_all() == settled


async def test_transport_only_refresh_never_supersedes_crypto_checkpoint(tmp_path):
    from tradepulse.reconciliation.fee_replay import replay_asset_fees

    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=received(raw, NOW))
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert epoch['fee_accounting_status'] == 'reconciled_net'
    records = await r.reconciliation_records.list_all()
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW + timedelta(minutes=5),
                            pagination=received(raw, NOW + timedelta(minutes=5)))
    assert (await r.accounting_epochs.list_all())[0]['payload'] == epoch
    assert await r.reconciliation_records.list_all() == records


def test_equity_evidence_window_starts_at_generation_opening():
    from tradepulse.verification.evidence import equity_evidence_window_start, timestamp

    start = timestamp('2026-09-29T14:51:58.436339+00:00')
    checkpoint = {'opened_at': '2026-09-29T14:51:53.589791+00:00'}
    assert equity_evidence_window_start(checkpoint, start) == timestamp(checkpoint['opened_at'])
    assert equity_evidence_window_start(None, start) == start


def test_unbound_snapshot_before_start_is_still_outside_generation():
    from python_tests.test_paper_verification import COSTS, START, passing_rows
    from python_tests.test_paper_verification import NOW as ASSESS_NOW
    from tradepulse.verification.evidence import assess

    rows = passing_rows()
    rows['equity_snapshots'].append({'snapshot_id': 'early', 'as_of': (START - timedelta(seconds=1)).isoformat(),
                                     'source': 'broker', 'total_equity': '10000'})
    assert 'equity_outside_broker_generation' in assess(rows, START.isoformat(), ASSESS_NOW, COSTS)['errors']


def test_soak_assessment_includes_runner_final_reconciliation_evidence():
    from tradepulse.verification.evidence import timestamp
    from tradepulse.verification.soak import _assessment_end

    segment_end = timestamp('2026-09-30T00:48:00.609469+00:00')
    rows = {'equity_snapshots': [{'as_of': '2026-09-30T00:48:29.753966+00:00'}],
            'reconciliation_records': [{'occurred_at': '2026-09-30T00:48:32.082159+00:00'}]}
    assert _assessment_end(rows, segment_end) == timestamp('2026-09-30T00:48:32.082159+00:00')
    assert _assessment_end({'equity_snapshots': [], 'reconciliation_records': []}, segment_end) == segment_end


def test_option_without_contract_multiplier_fails_closed():
    from tradepulse.models import AssetClass, AssetIdentity, contract_multiplier_of

    assert contract_multiplier_of(AssetIdentity('AAPL', AssetClass.EQUITY, 'alpaca:AAPL')) == Decimal(1)
    assert contract_multiplier_of(AssetIdentity('IWM261030C00286000', AssetClass.OPTION, 'alpaca:IWM261030C00286000',
                                                contract_multiplier=Decimal(100))) == Decimal(100)
    with pytest.raises(ValueError, match='option contract multiplier missing'):
        contract_multiplier_of(AssetIdentity('IWM261030C00286000', AssetClass.OPTION, 'alpaca:IWM261030C00286000'))
