"""Rev.154: delayed dispatch, operator absence proof, and late appearance."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_settlement_engine import _no_op_alerter, _repositories
from test_stranded_intents import BTC, NOW, _broker, _order, _payload, _stranded

from tradepulse.execution import has_in_flight_intent, reserve_symbol_for_execution
from tradepulse.models import TradeIntentStatus
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.reconciliation.coordinator import _recover_stranded_intents
from tradepulse.reconciliation.dispatch import (
    DispatchResolutionRefused,
    check_resolved_dispatches,
    resolve_dispatch,
)


async def _sent(repositories, age=timedelta(minutes=20), status=TradeIntentStatus.SUBMITTED):
    await _stranded(repositories, status=status, age=timedelta(hours=1))
    row = await repositories.trade_intents.get('ti-1')
    intent = replace(hydrate('trade_intents', row['payload']), submitted_at=NOW - age)
    await repositories.trade_intents.update('ti-1', intent, status=status.value)


def _lease():
    return SimpleNamespace(verify=lambda: None, ref=SimpleNamespace(account_number='PA1', venue='alpaca'))


def _activity_page(raw):
    return {'terminal': True, 'activity_ids': [r['id'] for r in raw],
            'request': {'page_size': '100', 'direction': 'asc'},
            'response_hash': sha256(encode_payload(raw).encode()).hexdigest()}


def _absent_broker():
    broker = _broker()
    broker.venue = 'alpaca'
    broker.get_order_by_client_order_id.return_value = None

    async def history(since, *, page_evidence):
        page_evidence.append({'terminal': True, 'order_ids': [], 'request': {'status': 'all', 'after': since.isoformat()}})
        return []

    async def activities(*, activity_type, page_evidence):
        page_evidence.append(_activity_page([]))
        return []
    broker.get_order_history.side_effect = history
    broker.get_activities.side_effect = activities
    return broker


async def test_request_held_in_transit_crash_then_late_acceptance_is_adopted(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    release = asyncio.Event()
    sent = asyncio.Event()
    broker = _absent_broker()
    visible = None

    async def in_transit():
        nonlocal visible
        sent.set()
        await release.wait()
        visible = _order()

    async def lookup(_):
        return visible
    broker.get_order_by_client_order_id.side_effect = lookup
    request = asyncio.create_task(in_transit())
    await sent.wait()
    try:
        # Gateway died: its dispatch write survives, while transport can finish.
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
        assert (await _payload(repositories))['status'] == 'submission_unknown'
        assert await has_in_flight_intent(repositories, BTC)
        release.set()
        await request
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
        assert (await _payload(repositories))['broker_order_id'] == 'order-9'
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_request_never_released_stays_unknown_through_repeated_passes(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    for age in (0, 1, 60, 1440):
        await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW + timedelta(minutes=age))
        assert (await _payload(repositories))['status'] == 'submission_unknown'
        assert await has_in_flight_intent(repositories, BTC)
    assert broker.get_order_by_client_order_id.await_count == 4


async def test_grace_counts_from_dispatch_not_creation(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories, age=timedelta(seconds=119))
    broker = _absent_broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW + timedelta(seconds=1)) == 1
    assert (await _payload(repositories))['status'] == 'submission_unknown'


async def test_manual_resolution_atomically_rejects_and_watches_then_late_appearance_holds(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    evidence = await resolve_dispatch(repositories, broker, 'ti-1', 'Checked broker activity', account_lease=_lease(), clock=lambda: NOW)
    assert evidence['exact_lookup'] == 'not_found_before_and_after_history'
    payload = await _payload(repositories)
    assert payload['rejection_reason'] == 'SUBMISSION_ABSENT_OPERATOR_RESOLVED'
    assert not await has_in_flight_intent(repositories, BTC)
    rows = await repositories.trade_intents.database.run(lambda c: c.execute('SELECT * FROM resolved_dispatch_watches').fetchall())
    assert len(rows) == 1
    assert rows[0]['expires_at'] == (NOW + timedelta(days=30)).isoformat()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.return_value = _order()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW) == 1
    assert (await _payload(repositories))['status'] == 'rejected'
    hold = await repositories.integrity_holds.get('order-9')
    assert hold['status'] == 'resolved_dispatch_appeared'
    # Re-open the database, and even after expiry the detected incident survives.
    restarted = await _repositories(tmp_path)
    broker.get_order_by_client_order_id.return_value = None
    assert await check_resolved_dispatches(restarted, broker, _no_op_alerter(), NOW + timedelta(days=31)) == 1


@pytest.mark.parametrize('fault', ['recent', 'legacy', 'account', 'present', 'timeout', 'pagination', 'fill', 'reason', 'reserved'])
async def test_manual_resolution_refuses_unproven_absence(tmp_path, fault):
    repositories = await _repositories(tmp_path)
    await _sent(repositories, age=timedelta(minutes=14) if fault == 'recent' else timedelta(minutes=20))
    broker = _absent_broker()
    reason = 'Investigated'
    if fault == 'legacy':
        intent = replace(hydrate('trade_intents', await _payload(repositories)), submitted_at=None)
        await repositories.trade_intents.update('ti-1', intent, status=intent.status.value)
    elif fault == 'account':
        broker.get_account.return_value = SimpleNamespace(account_number='OTHER')
    elif fault == 'present':
        broker.get_order_by_client_order_id.return_value = _order()
    elif fault == 'timeout':
        broker.get_order_history.side_effect = TimeoutError('read timeout')
    elif fault == 'pagination':
        broker.get_order_history.side_effect = None
        broker.get_order_history.return_value = []
    elif fault == 'fill':
        async def activities(*, activity_type, page_evidence):
            raw = {'id': 'late-fill', 'activity_type': 'FILL', 'symbol': 'BTCUSD',
                   'transaction_time': NOW.isoformat(), 'order_id': 'unknown'}
            page_evidence.append(_activity_page([raw]))
            return [SimpleNamespace(raw=raw)]
        broker.get_activities.side_effect = activities
    elif fault == 'reason':
        reason = ''
    elif fault == 'reserved':
        await reserve_symbol_for_execution(repositories.trade_intents.database, BTC, 'gateway')
    with pytest.raises((DispatchResolutionRefused, TimeoutError, ValueError)):
        await resolve_dispatch(repositories, broker, 'ti-1', reason, account_lease=_lease(), clock=lambda: NOW)
    assert (await _payload(repositories))['status'] == 'submitted'
    count = await repositories.trade_intents.database.run(lambda c: c.execute('SELECT COUNT(*) FROM resolved_dispatch_watches').fetchone()[0])
    assert count == 0


async def test_watch_expires_without_appearance(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    broker.get_order_by_client_order_id.reset_mock()
    assert await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW + timedelta(days=31)) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()


async def test_legacy_sent_intent_never_automatically_rejected(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    await _recover_stranded_intents(repositories, _absent_broker(), _no_op_alerter(), NOW)
    assert (await _payload(repositories))['status'] == 'submission_unknown'


def test_cli_exposes_resolve_dispatch():
    from tradepulse.cli import _build_parser
    args = _build_parser().parse_args(['resolve-dispatch', 'ti-1', '--reason', 'Investigated'])
    assert (args.command, args.intent_id, args.reason) == ('resolve-dispatch', 'ti-1', 'Investigated')


async def test_resolution_and_watch_roll_back_together(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    await repositories.trade_intents.database.run(lambda c: c.execute(
        "CREATE TRIGGER refuse_watch BEFORE INSERT ON resolved_dispatch_watches BEGIN SELECT RAISE(ABORT, 'disk error'); END"), write=True)
    with pytest.raises(Exception, match='disk error'):
        await resolve_dispatch(repositories, _absent_broker(), 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    assert (await _payload(repositories))['status'] == 'submitted'


async def test_resolution_refuses_lost_account_lease_before_commit(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    lease = _lease()
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise DispatchResolutionRefused('ACCOUNT_LEASE_LOST')
    lease.verify = verify
    with pytest.raises(DispatchResolutionRefused, match='ACCOUNT_LEASE_LOST'):
        await resolve_dispatch(repositories, _absent_broker(), 'ti-1', 'Investigated', account_lease=lease, clock=lambda: NOW)
    assert (await _payload(repositories))['status'] == 'submitted'


async def test_late_appearance_blocks_reconciliation_without_adoption(tmp_path):
    from tradepulse.reconciliation.coordinator import run_reconciliation

    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    broker.get_order_by_client_order_id.return_value = _order()
    summary = await run_reconciliation(repositories, broker, None, _no_op_alerter(), clock=lambda: NOW)
    assert summary.status == 'degraded' and summary.error == 'RESOLVED_DISPATCH_APPEARED'
    assert (await _payload(repositories))['status'] == 'rejected'
    # The hold exists even if no session was active before this pass.
    assert (await repositories.integrity_holds.get('order-9'))['status'] == 'resolved_dispatch_appeared'


async def test_watch_refuses_account_mismatch(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    broker.get_account.return_value = SimpleNamespace(account_number='OTHER')
    with pytest.raises(DispatchResolutionRefused, match='ACCOUNT_MISMATCH'):
        await check_resolved_dispatches(repositories, broker, _no_op_alerter(), NOW)


async def test_resolution_rechecks_exact_id_after_history(tmp_path):
    repositories = await _repositories(tmp_path)
    await _sent(repositories)
    broker = _absent_broker()
    broker.get_order_by_client_order_id.side_effect = [None, _order()]
    with pytest.raises(DispatchResolutionRefused, match='BROKER_ORDER_PRESENT'):
        await resolve_dispatch(repositories, broker, 'ti-1', 'Investigated', account_lease=_lease(), clock=lambda: NOW)
    assert (await _payload(repositories))['status'] == 'submitted'
