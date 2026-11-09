"""Rev.154 integration review (R154-01..05) of the reviewer's dispatch candidate.

R154-01: a late appearance holds and latches, but never stops the rest of the
         pass, and an operator acknowledgement closes it once the late order is
         terminal and every fill is accounted.
R154-02: evidence windows start DISPATCH_EVIDENCE_SKEW before the local
         dispatch time.
R154-03: the local dispatch time lives in dispatched_at; submitted_at stays the
         broker's acceptance time.
R154-04: SUBMITTED -> SUBMISSION_UNKNOWN is a critical alert, and a sent order
         that stays absent is alerted once per UTC day with one drift record.
R154-05: reconciliation counts integrity holds as valuation does, so a fully
         resolved SHORT_EXPOSURE hold no longer blocks the reset that releases it.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_dispatch_resolution import _absent_broker, _activity_page, _lease, _sent
from test_settlement_engine import _no_op_alerter, _repositories
from test_stranded_intents import NOW, _order, _payload, _recording_alerts

from tradepulse.models import IntegrityHold, IntegrityHoldType, TradeIntentStatus
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.reconciliation.coordinator import _recover_stranded_intents, run_reconciliation
from tradepulse.reconciliation.dispatch import (
    DISPATCH_EVIDENCE_SKEW,
    DispatchResolutionRefused,
    acknowledge_appearance,
    check_resolved_dispatches,
    resolve_dispatch,
)


def _reconcilable(broker):
    """The absent broker, able to take a whole reconciliation pass."""
    async def activities(*, activity_type=None, page_evidence=None, **_):
        if page_evidence is not None:
            page_evidence.append(_activity_page([]))
        return []
    broker.get_activities.side_effect = activities
    broker.get_positions.return_value = []
    return broker


async def _appeared(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _reconcilable(_absent_broker())
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    broker.get_order_by_client_order_id.return_value = _order()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW) == 1
    return repositories, broker


def _late_order(status, filled_qty):
    return SimpleNamespace(**{**vars(_order()), 'status': status, 'filled_qty': Decimal(filled_qty)})


def _with_fills(broker, *fills):
    async def activities(*, activity_type=None, page_evidence=None, **_):
        raw = [{'id': fill_id, 'activity_type': 'FILL', 'order_id': 'order-9', 'qty': qty, 'symbol': 'BTCUSD',
                'transaction_time': NOW.isoformat()} for fill_id, qty in fills]
        if page_evidence is not None:
            page_evidence.append(_activity_page(raw))
        return [SimpleNamespace(raw=r) for r in raw]
    broker.get_activities.side_effect = activities


async def _watch(repositories):
    return await repositories.trade_intents.database.run(
        lambda c: c.execute('SELECT * FROM resolved_dispatch_watches').fetchone())


# R154-01 ------------------------------------------------------------------

async def test_late_appearance_does_not_stop_the_rest_of_the_pass(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    summary = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert summary.status == 'degraded' and summary.error.startswith('RESOLVED_DISPATCH_APPEARED')
    # Positions and activities were still reconciled after the appearance.
    broker.get_positions.assert_awaited()
    assert broker.get_activities.await_count >= 1
    assert (await _payload(repositories))['status'] == 'rejected'  # never adopted


async def test_a_later_failure_never_hides_the_appearance(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    broker.get_positions.side_effect = RuntimeError('503')
    summary = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert summary.error.startswith('RESOLVED_DISPATCH_APPEARED; BROKER_POSITIONS_UNAVAILABLE')


async def test_an_unavailable_watch_lookup_degrades_but_continues(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _reconcilable(_absent_broker())
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    broker.get_order_by_client_order_id.side_effect = RuntimeError('lookup 503')
    summary = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert summary.status == 'degraded' and summary.error.startswith('DISPATCH_WATCH_UNAVAILABLE: lookup 503')
    broker.get_positions.assert_awaited()


@pytest.mark.parametrize('status', ['new', 'accepted', 'partially_filled', 'done_for_day', 'replaced'])
async def test_acknowledgement_refuses_an_order_that_can_still_change(tmp_path, status):
    repositories, broker = await _appeared(tmp_path)
    broker.get_order.return_value = _late_order(status, '0')
    with pytest.raises(DispatchResolutionRefused, match='APPEARED_ORDER_NOT_TERMINAL'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked', account_lease=_lease(), clock=lambda: NOW)
    assert (await repositories.integrity_holds.get('order-9'))['status'] == 'resolved_dispatch_appeared'


async def test_acknowledgement_refuses_unaccounted_or_missing_fills(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    broker.get_order.return_value = _late_order('filled', '1')
    _with_fills(broker)  # the broker reports none of the order's fills
    with pytest.raises(DispatchResolutionRefused, match='APPEARED_FILLS_INCOMPLETE'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked', account_lease=_lease(), clock=lambda: NOW)
    _with_fills(broker, ('late-fill-1', '1'))
    with pytest.raises(DispatchResolutionRefused, match='APPEARED_FILL_UNACCOUNTED: late-fill-1'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked', account_lease=_lease(), clock=lambda: NOW)
    assert (await _watch(repositories))['acknowledged_at'] is None
    assert await repositories.integrity_holds.get('order-9') is not None


async def test_acknowledgement_closes_an_accounted_terminal_appearance(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    broker.get_order.return_value = _late_order('filled', '1')
    _with_fills(broker, ('late-fill-1', '1'))
    # Accounted by exact ID, as Rev.140 quarantine resolution would record it.
    await repositories.trade_intents.database.run(lambda c: c.execute(
        'INSERT INTO quarantine_executions(record_id,payload,created_at) VALUES(?,?,?)',
        ('late-fill-1', encode_payload({'activity_id': 'late-fill-1'}), NOW.isoformat())), write=True)
    evidence = await acknowledge_appearance(repositories, broker, 'ti-1', 'Accounted via quarantine',
                                            account_lease=_lease(), clock=lambda: NOW)
    assert evidence['fill_activity_ids'] == ['late-fill-1'] and evidence['order_status'] == 'filled'
    assert await repositories.integrity_holds.get('order-9') is None
    assert (await _watch(repositories))['acknowledged_at'] == NOW.isoformat()
    broker.get_order_by_client_order_id.reset_mock()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW + timedelta(days=40)) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()
    with pytest.raises(DispatchResolutionRefused, match='APPEARANCE_ALREADY_ACKNOWLEDGED'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Again', account_lease=_lease(), clock=lambda: NOW)


async def test_acknowledgement_needs_an_appearance_and_a_reason(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    with pytest.raises(DispatchResolutionRefused, match='DISPATCH_NOT_APPEARED'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked', account_lease=_lease(), clock=lambda: NOW)
    with pytest.raises(DispatchResolutionRefused, match='OPERATOR_REASON_REQUIRED'):
        await acknowledge_appearance(repositories, broker, 'ti-1', ' ', account_lease=_lease(), clock=lambda: NOW)


async def test_acknowledgement_refuses_an_order_that_changes_during_the_read(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    broker.get_order.side_effect = [_late_order('canceled', '0'), _late_order('canceled', '1')]
    _with_fills(broker)
    with pytest.raises(DispatchResolutionRefused, match='APPEARED_ORDER_CHANGED'):
        await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked', account_lease=_lease(), clock=lambda: NOW)


async def test_an_acknowledgement_during_the_watch_lookup_is_never_undone(tmp_path):
    """R154-06 (the reviewer's sequence): a pass reads the active watch, then
    pauses in its broker lookup; the operator's acknowledgement commits,
    deleting the hold and closing the watch; the pass resumes. It must not
    recreate the hold from its stale row."""
    repositories, broker = await _appeared(tmp_path)
    broker.get_order.return_value = _late_order('canceled', '0')
    _with_fills(broker)
    acknowledged = []

    async def lookup_paused_by_the_acknowledgement(client_order_id):
        acknowledged.append(await acknowledge_appearance(repositories, broker, 'ti-1', 'Checked',
                                                         account_lease=_lease(), clock=lambda: NOW))
        return _order()
    broker.get_order_by_client_order_id.side_effect = lookup_paused_by_the_acknowledgement
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW) == 0
    assert acknowledged and acknowledged[0]['order_status'] == 'canceled'
    assert await repositories.integrity_holds.get('order-9') is None
    assert (await _watch(repositories))['acknowledged_at'] == NOW.isoformat()
    # And the closed watch stays closed: later passes skip it.
    broker.get_order_by_client_order_id.side_effect = None
    broker.get_order_by_client_order_id.reset_mock()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()


async def test_a_watch_whose_appeared_order_changes_is_refused(tmp_path):
    repositories, broker = await _appeared(tmp_path)
    broker.get_order_by_client_order_id.return_value = SimpleNamespace(**{**vars(_order()), 'broker_order_id': 'order-10'})
    with pytest.raises(DispatchResolutionRefused, match='DISPATCH_WATCH_ORDER_CHANGED'):
        await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW)
    assert await repositories.integrity_holds.get('order-10') is None


def test_cli_exposes_acknowledge_appearance():
    from tradepulse.cli import _build_parser
    args = _build_parser().parse_args(['resolve-dispatch', 'ti-1', '--reason', 'Checked', '--acknowledge-appearance'])
    assert args.acknowledge_appearance is True
    assert _build_parser().parse_args(['resolve-dispatch', 'ti-1', '--reason', 'x']).acknowledge_appearance is False


# R154-02 ------------------------------------------------------------------

async def test_evidence_windows_start_before_the_local_dispatch_time(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    dispatched = hydrate('trade_intents', await _payload(repositories)).dispatched_at
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    assert broker.get_order_history.await_args.args[0] == dispatched - DISPATCH_EVIDENCE_SKEW


async def test_a_broker_fill_stamped_before_the_local_dispatch_time_still_counts(tmp_path):
    """The local clock runs two minutes ahead of the broker's: the delayed
    order's fill carries an earlier broker time than dispatched_at."""
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    dispatched = hydrate('trade_intents', await _payload(repositories)).dispatched_at

    async def activities(*, activity_type, page_evidence):
        raw = {'id': 'skewed-fill', 'activity_type': 'FILL', 'symbol': 'BTCUSD', 'order_id': 'unknown',
               'transaction_time': (dispatched - timedelta(minutes=2)).isoformat()}
        page_evidence.append(_activity_page([raw]))
        return [SimpleNamespace(raw=raw)]
    broker.get_activities.side_effect = activities
    with pytest.raises(DispatchResolutionRefused, match='UNEXPLAINED_ASSET_FILL'):
        await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    assert (await _payload(repositories))['status'] == 'submitted'


# R154-03 ------------------------------------------------------------------

@pytest.mark.usefixtures('open_market_calendar')
async def test_acceptance_keeps_the_broker_time_and_the_dispatch_time_apart(tmp_path):
    from test_dbae_gateway import FRAC, _buy, _hold_orders_open, _rig

    rig = await _rig(tmp_path)
    rig.quote(FRAC, Decimal(100))
    _hold_orders_open(rig)
    broker_time = rig.now() + timedelta(seconds=3)

    async def stamped(order):
        placed = await rig.broker.place_on_book(order)
        return replace(placed, submitted_at=broker_time)
    rig.broker.place_hook = stamped
    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92), key='stamped-buy'))
    [intent] = await rig.intents()
    assert intent.broker_order_id is not None, (result, intent.status, intent.rejection_reason)
    assert intent.submitted_at == broker_time and intent.dispatched_at == rig.now()


async def test_legacy_rows_without_dispatched_at_hydrate(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    payload = dict(await _payload(repositories))
    payload.pop('dispatched_at')
    assert hydrate('trade_intents', payload).dispatched_at is None


# R154-04 ------------------------------------------------------------------

async def test_becoming_unknown_is_a_critical_alert_not_a_resolution(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    alerts = _recording_alerts()
    assert await _recover_stranded_intents(repositories, _absent_broker(), alerts, NOW) == 1
    severity, message = alerts.send.await_args.args[:2]
    assert severity == 'critical' and message.startswith('SUBMISSION_UNKNOWN')
    assert 'resolved' not in message.split('--')[0] and 'resolve-dispatch' in message


async def test_a_sent_order_that_stays_absent_is_alerted_once_per_day(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories, status=TradeIntentStatus.SUBMISSION_UNKNOWN)
    broker = _absent_broker()
    alerts = _recording_alerts()
    for minutes in (0, 1, 2, 60):
        await _recover_stranded_intents(repositories, broker, alerts, NOW + timedelta(minutes=minutes))
    assert alerts.send.await_count == 1 and alerts.send.await_args.args[0] == 'critical'
    drift = [r['payload'] for r in await repositories.reconciliation_records.list_all()]
    assert [r['actual']['error'] for r in drift] == ['SUBMISSION_ABSENCE_UNPROVEN']
    await _recover_stranded_intents(repositories, broker, alerts, NOW + timedelta(days=1))
    assert alerts.send.await_count == 2
    assert (await _payload(repositories))['status'] == 'submission_unknown'


# R154-05 ------------------------------------------------------------------

async def test_a_releasable_short_exposure_hold_does_not_degrade_reconciliation(tmp_path, monkeypatch):
    from tradepulse.quarantine import release

    repositories = await _repositories(tmp_path)
    broker = _reconcilable(_absent_broker())
    hold = IntegrityHold('short-order', 'ti-short', IntegrityHoldType.SHORT_EXPOSURE, 'oversell', NOW)
    await repositories.integrity_holds.database.run(lambda c: c.execute(
        'INSERT INTO integrity_holds(record_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?)',
        ('short-order', 'short_exposure', encode_payload(hold), NOW.isoformat(), NOW.isoformat())), write=True)
    held = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert held.error == 'ACTIVE_INTEGRITY_HOLD'
    # Once its exposure is fully resolved, reset-integrity releases it in its
    # own transaction; the reconciliation reset requires must not block that.
    monkeypatch.setattr(release, 'released_short_exposure_holds', lambda connection: ['short-order'])
    released = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert released.error != 'ACTIVE_INTEGRITY_HOLD'
