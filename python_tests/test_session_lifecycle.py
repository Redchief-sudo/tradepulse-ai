import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient
from tradepulse.session_commands import (
    run_reset_integrity as _run_reset_integrity,
    run_reset_risk as _run_reset_risk,
    run_start as _run_start,
    run_status as _run_status,
    run_stop as _run_stop,
)
from tradepulse.config import Settings
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    IntegrityHold,
    IntegrityHoldType,
    PositionLot,
    SessionState,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.risk import (
    SESSION_RECORD_ID,
    latch_financial_integrity_block,
    latch_risk_stop,
    load_session,
    save_session,
    sync_market_session,
    transition_session,
)

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _settings_with_creds(database_url: str, **extra: str) -> Settings:
    return Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "TRADEPULSE_DATABASE_URL": database_url, **extra})


def _settings_no_creds(database_url: str) -> Settings:
    return Settings.from_env({"TRADEPULSE_DATABASE_URL": database_url})


def _active(now: datetime = NOW) -> TradingSession:
    return TradingSession(SESSION_RECORD_ID, SessionState.ACTIVE, True, now)


def _manually_stopped(now: datetime = NOW) -> TradingSession:
    return TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, now)


def _risk_stopped(now: datetime = NOW, reason: str = "daily loss exceeded") -> TradingSession:
    return TradingSession(
        SESSION_RECORD_ID, SessionState.RISK_STOPPED, False, now,
        kill_switch_reason=reason, kill_switch_at=now, kill_switch_reset_required=True,
    )


def _integrity_blocked(now: datetime = NOW, reason: str = "broker position mismatch") -> TradingSession:
    return TradingSession(
        SESSION_RECORD_ID, SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, now,
        financial_integrity_reason=reason, financial_integrity_manual_reenable_required=True,
    )


def _system_degraded(now: datetime = NOW) -> TradingSession:
    return TradingSession(SESSION_RECORD_ID, SessionState.SYSTEM_DEGRADED, False, now)


def _market_closed(now: datetime = NOW) -> TradingSession:
    return TradingSession(SESSION_RECORD_ID, SessionState.MARKET_CLOSED, False, now)


def _broker_unavailable(now: datetime = NOW) -> TradingSession:
    return TradingSession(SESSION_RECORD_ID, SessionState.BROKER_UNAVAILABLE, False, now)


def _broker() -> AlpacaClient:
    return AlpacaClient("key", "secret", "paper", 10)


def _mock_clock(is_open: bool) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/clock").mock(
        return_value=httpx.Response(200, json={"is_open": is_open, "next_open": None, "next_close": None, "timestamp": NOW.isoformat().replace("+00:00", "Z")})
    )


def _mock_account_ok() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": "100000", "last_equity": "99500", "cash": "50000", "buying_power": "100000", "portfolio_value": "100000"})
    )


def _mock_account_failure() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(return_value=httpx.Response(500, json={"message": "internal error"}))


def _mock_clean_reconciliation() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=[]))


async def _seed_drifted_lot(repositories: PersistenceRepositories) -> None:
    """Local lots say 5 shares; the mocked broker position below says 10 --
    a genuine accounting_drift_detected > 0."""
    lot = PositionLot(
        lot_id="lot-1", originating_fill_id="fill-1", asset=_aapl(), position_side="long",
        opened_quantity=Decimal("5"), remaining_quantity=Decimal("5"), acquisition_price=Decimal("150"), opened_at=NOW,
    )
    await repositories.position_lots.create_once("lot-1", lot, unique_value="fill-1")


def _mock_dirty_reconciliation() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(200, json=[{
            "symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
            "market_value": "0", "current_price": "150", "unrealized_pl": "0",
        }])
    )
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=[]))


def _mock_missed_fill_reconciliation() -> None:
    """Positions agree (no accounting drift), but there's an orphaned fill
    activity with no matching local Fill and no known TradeIntent by
    order_id -- accounting_drift_detected stays 0, missed_fills_detected
    becomes 1."""
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=[{
            "id": "activity-orphan", "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
            "qty": "5", "price": "150", "transaction_time": NOW.isoformat().replace("+00:00", "Z"), "order_id": "order-does-not-exist",
        }])
    )


async def _audit_events(repositories: PersistenceRepositories) -> list[AuditEvent]:
    rows = await repositories.audit_events.list_all(limit=100)
    return [hydrate("audit_events", row["payload"]) for row in rows]


# ---- start ----------------------------------------------------------------

@respx.mock
async def test_start_activates_fresh_database(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    _mock_account_ok()

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0

    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE
    assert session.trading_active is True
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "start"


@respx.mock
async def test_start_activates_manually_stopped_session(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())
    _mock_account_ok()

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE


@respx.mock
async def test_start_refuses_with_broken_credentials(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())
    _mock_account_failure()

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 1
    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    assert (await _audit_events(repositories)) == []


@respx.mock
async def test_start_on_already_active_is_noop(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())
    # No get_account mock registered -- an unexpected call fails the test via respx.

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    assert (await _audit_events(repositories)) == []


@respx.mock
@pytest.mark.parametrize("seed", [_risk_stopped, _integrity_blocked, _system_degraded, _market_closed])
async def test_start_refuses_unconditionally_from_hard_blocked_states(tmp_path, seed) -> None:
    repositories = await _repositories(tmp_path)
    seeded = seed()
    await save_session(repositories, seeded)
    # No get_account mock registered -- refused before any health check.

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 1
    session = await load_session(repositories)
    assert session.state == seeded.state
    assert (await _audit_events(repositories)) == []


@respx.mock
async def test_start_from_broker_unavailable_with_healthy_check_activates(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _broker_unavailable())
    _mock_account_ok()

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE


@respx.mock
async def test_start_from_broker_unavailable_with_failing_check_stays_unavailable(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _broker_unavailable())
    _mock_account_failure()

    exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 1
    session = await load_session(repositories)
    assert session.state == SessionState.BROKER_UNAVAILABLE


# ---- stop -------------------------------------------------------------

async def test_stop_deactivates_active_session(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())

    exit_code = await _run_stop(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    assert session.trading_active is False
    events = await _audit_events(repositories)
    assert len(events) == 1 and events[0].details["action"] == "stop"


@pytest.mark.parametrize("seed", [_risk_stopped, _integrity_blocked])
async def test_stop_never_downgrades_a_safety_block(tmp_path, seed) -> None:
    repositories = await _repositories(tmp_path)
    seeded = seed()
    await save_session(repositories, seeded)

    exit_code = await _run_stop(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == seeded.state
    assert session.kill_switch_reason == seeded.kill_switch_reason
    assert session.financial_integrity_reason == seeded.financial_integrity_reason
    assert (await _audit_events(repositories)) == []


async def test_stop_succeeds_with_zero_credentials_configured(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())

    exit_code = await _run_stop(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED


# ---- status -------------------------------------------------------------

async def test_status_succeeds_with_zero_credentials_configured(tmp_path, caplog) -> None:
    import logging

    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())

    with caplog.at_level(logging.INFO):
        exit_code = await _run_status(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    status_records = [r for r in caplog.records if getattr(r, "event", None) == "session_status"]
    assert len(status_records) == 1
    assert status_records[0].state == "active"
    assert status_records[0].trading_active is True


async def test_status_reports_risk_stopped_reason(tmp_path, caplog) -> None:
    import logging

    repositories = await _repositories(tmp_path)
    await save_session(repositories, _risk_stopped(reason="daily loss exceeded"))

    with caplog.at_level(logging.INFO):
        await _run_status(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    status_records = [r for r in caplog.records if getattr(r, "event", None) == "session_status"]
    assert status_records[0].state == "risk_stopped"
    assert status_records[0].kill_switch_reason == "daily loss exceeded"
    assert status_records[0].kill_switch_reset_required is True


# ---- reset-risk -----------------------------------------------------------

async def test_reset_risk_clears_kill_switch_and_permits_subsequent_start(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _risk_stopped(reason="daily loss exceeded"))

    exit_code = await _run_reset_risk(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0

    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    assert session.kill_switch_reset_required is False
    assert session.kill_switch_reason is None
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "reset_risk"
    assert events[0].details["reason"] == "daily loss exceeded"

    with respx.mock:
        _mock_account_ok()
        assert await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db")) == 0
    assert (await load_session(repositories)).state == SessionState.ACTIVE


async def test_reset_risk_does_not_clear_financial_integrity_block(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    seeded = _integrity_blocked()
    await save_session(repositories, seeded)

    exit_code = await _run_reset_risk(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert session.financial_integrity_manual_reenable_required is True
    assert (await _audit_events(repositories)) == []


async def test_ordinary_session_transitions_never_touch_integrity_holds(tmp_path) -> None:
    """Locks down the new opt-in boundary: transition_session's default is
    clear_integrity_holds=False, and every ordinary caller (start/stop/
    reset-risk/anything future) must go on relying on that default. Proves
    it end to end through a real, unrelated session command (reset-risk)
    rather than calling transition_session directly, so a future caller
    that forgets to pass the flag explicitly is still covered."""
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _risk_stopped(reason="daily loss exceeded"))
    hold = IntegrityHold(
        broker_order_id="order-1", trade_intent_id="ti-1", hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED,
        reason="INTEGRITY_VIOLATION: disputed", created_at=NOW,
    )
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)
    hold_row_before = await repositories.integrity_holds.get("order-1")

    exit_code = await _run_reset_risk(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"))
    assert exit_code == 0
    assert (await load_session(repositories)).state == SessionState.MANUALLY_STOPPED  # the transition itself did happen

    hold_row_after = await repositories.integrity_holds.get("order-1")
    assert hold_row_after == hold_row_before  # byte-for-byte untouched


# ---- reset-integrity -------------------------------------------------------

@respx.mock
async def test_reset_integrity_clears_after_clean_reconciliation(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="broker position mismatch"))
    _mock_clean_reconciliation()

    exit_code = await _run_reset_integrity(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"), force=False)
    assert exit_code == 0

    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    assert session.financial_integrity_manual_reenable_required is False
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "reset_integrity"
    assert events[0].severity == "info"
    assert events[0].details["accounting_drift_detected"] == "0"

    with respx.mock:
        _mock_account_ok()
        assert await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db")) == 0
    assert (await load_session(repositories)).state == SessionState.ACTIVE


@respx.mock
async def test_reset_integrity_refuses_while_accounting_drift_persists(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    seeded = _integrity_blocked()
    await save_session(repositories, seeded)
    await _seed_drifted_lot(repositories)
    _mock_dirty_reconciliation()

    exit_code = await _run_reset_integrity(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"), force=False)
    assert exit_code == 1
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert (await _audit_events(repositories)) == []


@respx.mock
async def test_reset_integrity_refuses_non_force_while_an_integrity_hold_remains(tmp_path) -> None:
    """FIN-095-02: a clean POSITION-level reconciliation is not evidence
    that a specific order-level fill-quantity dispute was ever actually
    explained -- positions can balance in aggregate while the underlying
    per-order anomaly stays factually unresolved (e.g. a phantom/erroneous
    Activities entry with no real extra broker holding behind it). The
    non-force path must refuse to clear a still-active hold just because
    reconciliation happens to report clean; only --force (already an
    explicitly logged unverified action) may do that."""
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="order fill quantity disputed"))
    hold = IntegrityHold(
        broker_order_id="order-1", trade_intent_id="ti-1", hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED,
        reason="INTEGRITY_VIOLATION: disputed", created_at=NOW,
    )
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)
    _mock_clean_reconciliation()  # positions/activities both report perfectly clean

    exit_code = await _run_reset_integrity(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"), force=False)
    assert exit_code == 1

    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED  # never cleared
    assert await repositories.integrity_holds.get("order-1") is not None  # hold still present
    assert (await _audit_events(repositories)) == []

    # --force remains the only path that can clear it when nothing has
    # actually resolved the dispute -- unchanged, already-existing contract.
    with respx.mock:
        forced_exit_code = await _run_reset_integrity(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"), force=True)
    assert forced_exit_code == 0
    assert (await load_session(repositories)).state == SessionState.MANUALLY_STOPPED
    assert await repositories.integrity_holds.get("order-1") is None


async def test_reset_integrity_force_clears_without_reconciliation(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="broker position mismatch"))
    # No broker mocks registered at all -- proves no reconciliation call is made.

    exit_code = await _run_reset_integrity(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"), force=True)
    assert exit_code == 0

    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "reset_integrity_forced"
    assert events[0].severity == "critical"


async def test_reset_integrity_clears_persisted_integrity_holds(tmp_path) -> None:
    """FIN-095-02: a fill_quantity_disputed hold left in place after
    reset-integrity would silently re-trigger latch_financial_integrity_block
    on the very next legitimate fill for that order, with no symptom
    pointing at the real (stale-hold) cause -- reset-integrity must clear
    every integrity_holds row, not just the session-level state."""
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="order fill quantity disputed"))
    hold = IntegrityHold(
        broker_order_id="order-1", trade_intent_id="ti-1", hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED,
        reason="INTEGRITY_VIOLATION: disputed", created_at=NOW,
    )
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)

    exit_code = await _run_reset_integrity(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"), force=True)
    assert exit_code == 0
    assert await repositories.integrity_holds.get("order-1") is None


async def test_reset_integrity_fails_closed_when_the_transaction_fails_after_holds_are_queued(tmp_path, monkeypatch) -> None:
    """The exact interleaving flagged as a residual gap: holds cleared as
    a separate, already-committed step, then the session-state write
    fails -- would leave the session correctly still
    FINANCIAL_INTEGRITY_BLOCKED while the settlement guard's only evidence
    of a still-genuinely-disputed order was already gone. Clearing holds
    and transitioning the session are now ONE transaction
    (transition_session's clear_integrity_holds=True), so a failure
    injected AFTER the hold-deletion SQL has been issued (but before the
    transaction commits) must roll back BOTH -- the hold must still exist
    afterward, not just the session state."""
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="order fill quantity disputed"))
    hold = IntegrityHold(
        broker_order_id="order-1", trade_intent_id="ti-1", hold_type=IntegrityHoldType.FILL_QUANTITY_DISPUTED,
        reason="INTEGRITY_VIOLATION: disputed", created_at=NOW,
    )
    await repositories.integrity_holds.create_once("order-1", hold, status=hold.hold_type.value)

    # Fails encode_payload specifically for the new TradingSession value --
    # inside transition_session's op(), this is called AFTER the
    # integrity_holds DELETE statements have already been issued on the
    # same (uncommitted) connection, but BEFORE the session row itself is
    # written -- exactly the ordering the user's scenario depends on.
    from tradepulse.risk import session as session_module
    original_encode_payload = session_module.encode_payload

    def _boom(value):
        if isinstance(value, TradingSession) and value.state == SessionState.MANUALLY_STOPPED:
            raise RuntimeError("synthetic failure between hold-deletion and session-write")
        return original_encode_payload(value)

    monkeypatch.setattr(session_module, "encode_payload", _boom)

    exit_code = await _run_reset_integrity(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"), force=True)
    assert exit_code == 1

    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED  # never transitioned
    assert await repositories.integrity_holds.get("order-1") is not None  # hold survives -- the DELETE rolled back too
    assert (await _audit_events(repositories)) == []  # no session-transition audit event was ever written


async def test_reset_integrity_does_not_clear_risk_stop(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    seeded = _risk_stopped()
    await save_session(repositories, seeded)

    exit_code = await _run_reset_integrity(_settings_no_creds(f"sqlite:///{tmp_path}/test.db"), force=True)
    assert exit_code == 0
    session = await load_session(repositories)
    assert session.state == SessionState.RISK_STOPPED
    assert (await _audit_events(repositories)) == []


# ---- transition_session atomicity -----------------------------------------

async def test_concurrent_identical_transitions_commit_exactly_once(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())

    def decide(session: TradingSession):
        if session.state != SessionState.MANUALLY_STOPPED:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.ACTIVE, True, NOW)
        event = AuditEvent(
            event_id=f"evt-{id(session)}-{session.state.value}", event_type="session_transition", severity="info",
            message="test", occurred_at=NOW, entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={"action": "start"},
        )
        return new_session, event

    results = await asyncio.gather(
        transition_session(repositories, decide), transition_session(repositories, decide),
    )
    successes = [r for r in results if r is not None]
    assert len(successes) == 1  # exactly one real transition
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE
    events = await _audit_events(repositories)
    assert len(events) == 1  # at most one transition audit


async def test_concurrent_mixed_transitions_serialize_without_torn_state(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())

    def start_decide(session: TradingSession):
        if session.state != SessionState.MANUALLY_STOPPED:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.ACTIVE, True, NOW)
        event = AuditEvent(
            event_id="evt-start", event_type="session_transition", severity="info", message="start", occurred_at=NOW,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID, details={"action": "start", "previous_state": session.state.value},
        )
        return new_session, event

    def stop_decide(session: TradingSession):
        if session.state == SessionState.MANUALLY_STOPPED and not session.trading_active:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, NOW)
        event = AuditEvent(
            event_id="evt-stop", event_type="session_transition", severity="info", message="stop", occurred_at=NOW,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID, details={"action": "stop", "previous_state": session.state.value},
        )
        return new_session, event

    await asyncio.gather(transition_session(repositories, start_decide), transition_session(repositories, stop_decide))

    session = await load_session(repositories)
    assert session.state in (SessionState.ACTIVE, SessionState.MANUALLY_STOPPED)  # never a torn/invalid state
    events = await _audit_events(repositories)
    assert 1 <= len(events) <= 2
    for event in events:
        assert event.details["previous_state"] in ("manually_stopped", "active")  # trail matches what was actually committed


async def test_transition_session_rolls_back_fully_on_decide_failure(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())

    def failing_decide(session: TradingSession):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await transition_session(repositories, failing_decide)

    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED  # unchanged
    assert (await _audit_events(repositories)) == []  # no orphaned audit event


# ---- latch_risk_stop / latch_financial_integrity_block ---------------------

@pytest.mark.parametrize("seed", [_active, _manually_stopped])
async def test_latch_risk_stop_transitions_into_risk_stopped(tmp_path, seed) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, seed())

    result = await latch_risk_stop(repositories, "daily loss exceeded", clock=lambda: NOW)
    assert result is not None
    assert result.state == SessionState.RISK_STOPPED
    assert result.kill_switch_reason == "daily loss exceeded"
    assert result.kill_switch_reset_required is True
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "latch_risk_stop"
    assert events[0].severity == "critical"


async def test_latch_risk_stop_is_idempotent_and_preserves_original_reason(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _risk_stopped(reason="first breach"))

    result = await latch_risk_stop(repositories, "second breach", clock=lambda: NOW)
    assert result is None
    session = await load_session(repositories)
    assert session.kill_switch_reason == "first breach"  # not overwritten
    assert (await _audit_events(repositories)) == []


async def test_latch_risk_stop_never_downgrades_financial_integrity_block(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    seeded = _integrity_blocked()
    await save_session(repositories, seeded)

    result = await latch_risk_stop(repositories, "daily loss exceeded", clock=lambda: NOW)
    assert result is None
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert (await _audit_events(repositories)) == []


async def test_latch_financial_integrity_block_escalates_from_risk_stopped(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _risk_stopped(reason="daily loss exceeded"))

    result = await latch_financial_integrity_block(repositories, "accounting drift", clock=lambda: NOW)
    assert result is not None
    assert result.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert result.financial_integrity_reason == "accounting drift"
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "latch_integrity_block"
    assert events[0].severity == "critical"


async def test_latch_financial_integrity_block_is_idempotent(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked(reason="first problem"))

    result = await latch_financial_integrity_block(repositories, "second problem", clock=lambda: NOW)
    assert result is None
    session = await load_session(repositories)
    assert session.financial_integrity_reason == "first problem"
    assert (await _audit_events(repositories)) == []


# ---- reset-integrity missed-fills gate -------------------------------------

@respx.mock
async def test_reset_integrity_refuses_while_missed_fill_persists_even_with_zero_drift(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _integrity_blocked())
    _mock_missed_fill_reconciliation()

    exit_code = await _run_reset_integrity(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"), force=False)
    assert exit_code == 1
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED


# ---- start: broader broker-unreachable handling ----------------------------

async def test_start_refuses_cleanly_on_raw_transport_error(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _manually_stopped())

    with respx.mock:
        respx.get("https://paper-api.alpaca.markets/v2/account").mock(side_effect=httpx.ConnectError("connection refused"))
        exit_code = await _run_start(_settings_with_creds(f"sqlite:///{tmp_path}/test.db"))

    assert exit_code == 1  # no uncaught traceback
    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED


# ---- sync_market_session ----------------------------------------------------

async def test_sync_market_session_transitions_active_to_market_closed(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())
    broker = _broker()

    with respx.mock:
        _mock_clock(is_open=False)
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is not None
    assert result.state == SessionState.MARKET_CLOSED
    assert result.trading_active is True  # preserved -- crypto keeps trading continuously
    events = await _audit_events(repositories)
    assert len(events) == 1
    assert events[0].details["action"] == "sync_market_session"
    assert events[0].severity == "info"  # routine, not a safety event


async def test_sync_market_session_transitions_market_closed_to_active(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _market_closed())
    broker = _broker()

    with respx.mock:
        _mock_clock(is_open=True)
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is not None
    assert result.state == SessionState.ACTIVE


@pytest.mark.parametrize(("seed", "is_open"), [(_active, True), (_market_closed, False)])
async def test_sync_market_session_is_a_noop_when_already_correct(tmp_path, seed, is_open) -> None:
    repositories = await _repositories(tmp_path)
    seeded = seed()
    await save_session(repositories, seeded)
    broker = _broker()

    with respx.mock:
        _mock_clock(is_open=is_open)
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is None
    session = await load_session(repositories)
    assert session.state == seeded.state
    assert (await _audit_events(repositories)) == []


@pytest.mark.parametrize(
    "seed", [_manually_stopped, _risk_stopped, _integrity_blocked, _system_degraded, _broker_unavailable]
)
@pytest.mark.parametrize("is_open", [True, False])
async def test_sync_market_session_never_touches_operator_or_safety_states(tmp_path, seed, is_open) -> None:
    repositories = await _repositories(tmp_path)
    seeded = seed()
    await save_session(repositories, seeded)
    broker = _broker()

    with respx.mock:
        _mock_clock(is_open=is_open)
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is None
    session = await load_session(repositories)
    assert session.state == seeded.state
    assert session.kill_switch_reason == seeded.kill_switch_reason
    assert session.financial_integrity_reason == seeded.financial_integrity_reason
    assert (await _audit_events(repositories)) == []


async def test_sync_market_session_handles_alpaca_error_without_crashing(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())
    broker = _broker()

    with respx.mock:
        respx.get("https://paper-api.alpaca.markets/v2/clock").mock(return_value=httpx.Response(500, json={"message": "internal error"}))
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is None
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE  # unchanged, not guessed


async def test_sync_market_session_handles_raw_transport_error_without_crashing(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, _active())
    broker = _broker()

    with respx.mock:
        respx.get("https://paper-api.alpaca.markets/v2/clock").mock(side_effect=httpx.ConnectError("connection refused"))
        result = await sync_market_session(repositories, broker, clock=lambda: NOW)
    await broker.aclose()

    assert result is None
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE
