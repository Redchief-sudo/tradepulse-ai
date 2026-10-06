"""Rev.115 F1: resolve intents stranded between RISK_APPROVED and broker acceptance."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_settlement_engine import _no_op_alerter, _repositories
from tradepulse.execution import has_in_flight_intent, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    IntegrityHold,
    IntegrityHoldType,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import hydrate
from tradepulse.reconciliation.coordinator import _recover_stranded_intents

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)
BTC = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")


def _broker(account_number="PA1"):
    broker = AsyncMock()
    broker.get_account.return_value = SimpleNamespace(account_number=account_number)
    return broker


def _order(**overrides):
    fields = {"broker_order_id": "order-9", "symbol": "BTC/USD", "side": Side.SELL,
              "raw": {"client_order_id": "ti-1", "qty": "1", "type": "market"}}
    return SimpleNamespace(**{**fields, **overrides})


async def _stranded(repositories, status=TradeIntentStatus.RISK_APPROVED, age=timedelta(minutes=10), account="PA1"):
    snapshot = {} if account is None else {"broker_account_number": account}
    intent = TradeIntent("ti-1", "idem-1", "corr-1", BTC, Side.SELL, ExecutionMode.PAPER, "position_monitor",
                         NOW - age, requested_quantity=Decimal(1), status=status, risk_snapshot=snapshot)
    await repositories.trade_intents.create_once("ti-1", intent, status=status.value, unique_value="idem-1")


async def _payload(repositories):
    return (await repositories.trade_intents.get("ti-1"))["payload"]


def _hold(order_id, intent_id):
    return IntegrityHold(broker_order_id=order_id, trade_intent_id=intent_id,
                         hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED, reason="INTEGRITY_VIOLATION: disputed",
                         created_at=NOW)


async def test_sweep_closes_intent_alpaca_never_received(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert (await _payload(repositories))["status"] == "rejected"
    assert await has_in_flight_intent(repositories, BTC) is False
    broker.get_order_by_client_order_id.assert_awaited_once_with("ti-1")


async def test_sweep_adopts_matching_order_alpaca_did_receive(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = _order()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    payload = await _payload(repositories)
    assert (payload["status"], payload["broker_order_id"]) == ("accepted", "order-9")


async def test_sweep_refuses_when_account_identity_is_unproven(tmp_path):
    for recorded, current in ((None, "PA1"), ("PA1", "PA2"), ("", ""), ("PA1", ""), ("", "PA1")):
        repositories = await _repositories(tmp_path / f"{recorded}-{current}")
        await _stranded(repositories, account=recorded)
        broker = _broker(account_number=current)
        broker.get_order_by_client_order_id.return_value = None
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "risk_approved"
        broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_refuses_to_adopt_an_order_that_does_not_match(tmp_path):
    mismatches = ({"symbol": "ETH/USD"}, {"side": Side.BUY},
                  {"raw": {"client_order_id": "someone-else", "qty": "1", "type": "market"}},
                  {"raw": {"client_order_id": "ti-1", "qty": "2", "type": "market"}},     # Rev.120: quantity
                  {"raw": {"client_order_id": "ti-1", "qty": "1", "type": "limit"}},      # Rev.120: order type
                  {"raw": {"client_order_id": "ti-1", "notional": "1", "type": "market"}},  # no qty at all
                  {"raw": {"client_order_id": "ti-1", "qty": "abc", "type": "market"}})   # malformed
    for i, mismatch in enumerate(mismatches):
        repositories = await _repositories(tmp_path / f"case-{i}")
        await _stranded(repositories)
        broker = _broker()
        broker.get_order_by_client_order_id.return_value = _order(**mismatch)
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "risk_approved"


async def test_sweep_lookup_error_leaves_intent_unchanged(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.side_effect = RuntimeError("503")
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"
    records = [r["payload"] for r in await repositories.reconciliation_records.list_all()]
    assert [r["outcome"] for r in records] == ["drift_detected"]


async def test_sweep_ignores_young_intent(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, age=timedelta(seconds=30))
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()


async def test_sweep_skips_asset_with_live_reservation(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    assert await reserve_symbol_for_execution(repositories.trade_intents.database, BTC, "live-gateway")
    broker = _broker()
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    broker.get_order_by_client_order_id.assert_not_awaited()
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_hold_on_the_recovered_intent_or_adopted_order_blocks_recovery(tmp_path):
    for i, (order_id, intent_id) in enumerate((("unrelated-key", "ti-1"), ("order-9", "other"))):
        repositories = await _repositories(tmp_path / f"hold-{i}")
        await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
        hold = _hold(order_id, intent_id)
        await repositories.integrity_holds.create_once(order_id, hold, status=hold.hold_type.value)
        before = await repositories.integrity_holds.get(order_id)
        broker = _broker()
        broker.get_order_by_client_order_id.return_value = _order()
        assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
        assert (await _payload(repositories))["status"] == "submitted"
        assert await repositories.integrity_holds.get(order_id) == before


async def test_sweep_preserves_unrelated_holds_and_session_latch(tmp_path):
    from tradepulse.risk import latch_financial_integrity_block, load_session

    repositories = await _repositories(tmp_path)
    await latch_financial_integrity_block(repositories, "pre-existing latch", clock=lambda: NOW)
    hold = _hold("order-1", "other")
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)
    hold_before, session_before = await repositories.integrity_holds.get("order-1"), await load_session(repositories)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert await repositories.integrity_holds.get("order-1") == hold_before
    assert await load_session(repositories) == session_before


async def test_lease_lost_during_slow_lookup_writes_nothing(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)

    async def renewal_fails(*args, **kwargs):
        return False

    monkeypatch.setattr("tradepulse.persistence.lock.renew_lock", renewal_fails)
    broker = _broker()

    async def slow_lookup(client_order_id):
        await asyncio.sleep(1.5)  # outlives one heartbeat: max(ttl/3, 1) = 1 s at ttl 3
        return None

    broker.get_order_by_client_order_id.side_effect = slow_lookup
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW, lock_ttl_seconds=3) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"
    assert await repositories.reconciliation_records.list_all() == []  # fenced: not even a drift record


async def test_same_status_payload_change_is_never_overwritten(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()

    async def lookup_while_payload_changes(client_order_id):
        current = hydrate("trade_intents", (await repositories.trade_intents.get("ti-1"))["payload"])
        changed = replace(current, requested_quantity=Decimal(2))  # same status, different content
        await repositories.trade_intents.update("ti-1", changed, status=changed.status.value)
        return None

    broker.get_order_by_client_order_id.side_effect = lookup_while_payload_changes
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    payload = await _payload(repositories)
    assert (payload["status"], payload["requested_quantity"]) == ("submitted", "2")


async def test_stolen_reservation_is_refused_inside_the_commit(tmp_path):
    from tradepulse.execution import execution_lock_key

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    database = repositories.trade_intents.database

    async def lookup_while_reservation_is_stolen(client_order_id):
        # The renewal callback has not fired yet; only the commit's own check can catch this.
        await database.run(lambda c: c.execute("UPDATE locks SET owner_token='thief' WHERE lock_key=?",
                                                (execution_lock_key(BTC),)), write=True)
        return None

    broker.get_order_by_client_order_id.side_effect = lookup_while_reservation_is_stolen
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_lost_parent_reconcile_lease_is_refused_inside_the_commit(tmp_path):
    from tradepulse.persistence import acquire_lock

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    database = repositories.trade_intents.database
    assert await acquire_lock(database, "reconcile", "someone-else", "reconcile", 600)  # not our lease
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW,
                                           reconcile_lease=("reconcile", "our-token")) == 0
    assert (await _payload(repositories))["status"] == "risk_approved"


async def test_concurrent_status_change_is_never_overwritten(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, status=TradeIntentStatus.SUBMITTED)
    broker = _broker()

    async def lookup_while_gateway_advances(client_order_id):
        current = hydrate("trade_intents", (await repositories.trade_intents.get("ti-1"))["payload"])
        advanced = replace(current, status=TradeIntentStatus.ACCEPTED, broker_order_id="order-live")
        await repositories.trade_intents.update("ti-1", advanced, status=advanced.status.value)
        return None  # a "not found" that is stale relative to the competitor's write

    broker.get_order_by_client_order_id.side_effect = lookup_while_gateway_advances
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 0
    payload = await _payload(repositories)
    assert (payload["status"], payload["broker_order_id"]) == ("accepted", "order-live")


ETH = AssetIdentity("ETH/USD", AssetClass.CRYPTO, "alpaca:ETH/USD")


async def _second_stranded(repositories):
    intent = TradeIntent("ti-2", "idem-2", "corr-2", ETH, Side.SELL, ExecutionMode.PAPER, "position_monitor",
                         NOW - timedelta(minutes=10), requested_quantity=Decimal(1),
                         status=TradeIntentStatus.RISK_APPROVED, risk_snapshot={"broker_account_number": "PA1"})
    await repositories.trade_intents.create_once("ti-2", intent, status=intent.status.value, unique_value="idem-2")


def _poison_btc_reservation(monkeypatch):
    import tradepulse.execution as execution

    real = execution.reserve_symbol_for_execution

    async def reserve(database, asset, token, *args, **kwargs):
        if asset == BTC:
            raise RuntimeError("database is locked")
        return await real(database, asset, token, *args, **kwargs)

    monkeypatch.setattr(execution, "reserve_symbol_for_execution", reserve)


async def test_one_failing_candidate_does_not_stop_the_next_and_leaves_drift_evidence(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    await _second_stranded(repositories)
    _poison_btc_reservation(monkeypatch)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1
    assert (await _payload(repositories))["status"] == "risk_approved"
    assert (await repositories.trade_intents.get("ti-2"))["payload"]["status"] == "rejected"
    drift = [r["payload"] for r in await repositories.reconciliation_records.list_all()
             if r["payload"]["outcome"] == "drift_detected"]
    assert [(r["subject_id"], "database is locked" in r["actual"]["error"]) for r in drift] == [("ti-1", True)]


async def test_unhydratable_row_is_recorded_and_skipped(tmp_path, monkeypatch):
    import tradepulse.reconciliation.coordinator as coordinator

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    await _second_stranded(repositories)
    real = coordinator.hydrate

    def hydrate_or_fail(table, payload):
        if payload.get("trade_intent_id") == "ti-1":
            raise ValueError("legacy row")
        return real(table, payload)

    monkeypatch.setattr(coordinator, "hydrate", hydrate_or_fail)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 1


async def test_release_failure_does_not_abort_the_sweep(tmp_path, monkeypatch):
    import tradepulse.execution as execution

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    await _second_stranded(repositories)

    async def broken_release(*args, **kwargs):
        raise RuntimeError("release failed")

    monkeypatch.setattr(execution, "release_symbol_reservation", broken_release)
    broker = _broker()
    broker.get_order_by_client_order_id.return_value = None
    assert await _recover_stranded_intents(repositories, broker, _no_op_alerter(), NOW) == 2


async def test_run_reconciliation_still_reaches_protective_lanes_when_the_sweep_hits_an_error(tmp_path, monkeypatch):
    import pytest

    import tradepulse.reconciliation.coordinator as coordinator

    class Reached(Exception):
        pass

    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    _poison_btc_reservation(monkeypatch)
    inflight = AsyncMock(side_effect=Reached)  # stops the pass once the next lane is reached
    monkeypatch.setattr(coordinator, "_recover_inflight_orders", inflight)
    with pytest.raises(Reached):
        await coordinator.run_reconciliation(repositories, _broker(), None, _no_op_alerter(), clock=lambda: NOW)
    inflight.assert_awaited_once()


def _recording_alerts():
    return SimpleNamespace(send=AsyncMock())


async def _audit_ids(repositories):
    return [row["record_id"] for row in await repositories.audit_events.list_all()]


async def test_unprovable_intent_alerts_once_per_utc_day_with_one_drift_record(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, account=None)  # pre-Rev.115 intent: no broker_account_number
    broker = _broker()
    alerts = _recording_alerts()
    assert await _recover_stranded_intents(repositories, broker, alerts, NOW) == 0
    assert await _recover_stranded_intents(repositories, broker, alerts, NOW + timedelta(minutes=1)) == 0
    assert alerts.send.await_count == 1
    severity, message = alerts.send.await_args.args[:2]
    assert severity == "critical"
    assert "protective exits" in message and "manual resolution" in message
    assert await _audit_ids(repositories) == ["stranded_intent_unresolved:ti-1:2026-10-01"]
    records = [r["payload"] for r in await repositories.reconciliation_records.list_all()]
    assert [r["outcome"] for r in records] == ["drift_detected"]
    assert records[0]["actual"]["error"] == "ACCOUNT_IDENTITY_UNPROVEN"


async def test_unprovable_intent_alerts_again_on_a_later_utc_day(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories, account=None)
    broker = _broker()
    alerts = _recording_alerts()
    await _recover_stranded_intents(repositories, broker, alerts, NOW)
    await _recover_stranded_intents(repositories, broker, alerts, NOW + timedelta(days=1))
    assert alerts.send.await_count == 2
    assert sorted(await _audit_ids(repositories)) == ["stranded_intent_unresolved:ti-1:2026-10-01",
                                                     "stranded_intent_unresolved:ti-1:2026-10-02"]
    assert len(await repositories.reconciliation_records.list_all()) == 2


async def test_transient_lookup_error_keeps_per_pass_drift_and_sends_no_alert(tmp_path):
    repositories = await _repositories(tmp_path)
    await _stranded(repositories)
    broker = _broker()
    broker.get_order_by_client_order_id.side_effect = RuntimeError("503")
    alerts = _recording_alerts()
    await _recover_stranded_intents(repositories, broker, alerts, NOW)
    await _recover_stranded_intents(repositories, broker, alerts, NOW + timedelta(minutes=1))
    assert alerts.send.await_count == 0
    assert await _audit_ids(repositories) == []
    assert len(await repositories.reconciliation_records.list_all()) == 2
