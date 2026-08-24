import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from tradepulse.cli import (
    _run_reset_integrity,
    _run_reset_risk,
    _run_start,
    _run_status,
    _run_stop,
)
from tradepulse.config import Settings
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    PositionLot,
    SessionState,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.risk import SESSION_RECORD_ID, load_session, save_session, transition_session

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
