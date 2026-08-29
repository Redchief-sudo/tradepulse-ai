"""The trading session's sole control-plane authority -- start/stop/status/
reset-risk/reset-integrity, plus the composition-root helpers they share.

Extracted out of cli.py so the CLI and the local dashboard (tradepulse/web/)
call the EXACT same functions, never two independent implementations of the
same state machine. cli.py's own commands are thin wrappers around this
module; nothing here is CLI-specific (no argparse, no process exit-code
framing beyond the plain `int` return every caller already expects).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.config import Settings, SettingsError, risk_limits_for_profile
from tradepulse.execution import ExecutionGateway
from tradepulse.models import AuditEvent, ExecutionMode, SessionState, TradingSession
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation import run_reconciliation
from tradepulse.risk import SESSION_RECORD_ID, load_session, transition_session
from tradepulse.settlement import SettlementProcessor

logger = logging.getLogger(__name__)

# States `start` refuses unconditionally: RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED
# need their own explicit reset-risk/reset-integrity command first;
# SYSTEM_DEGRADED/MARKET_CLOSED are system-derived states this command has
# no way to safely verify have actually cleared.
_START_HARD_BLOCKED_STATES = frozenset(
    {SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED, SessionState.SYSTEM_DEGRADED, SessionState.MARKET_CLOSED}
)
# `stop` must never downgrade an active safety block into a plain
# MANUALLY_STOPPED -- that would erase the reason and the reset
# requirement, letting a bare `start` through even though nothing was reset.
_STOP_PRESERVED_STATES = frozenset({SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED})


def require_credentials(settings: Settings, *, require_ai: bool) -> None:
    checks = [("ALPACA_API_KEY", settings.alpaca_api_key), ("ALPACA_API_SECRET", settings.alpaca_api_secret)]
    if require_ai:
        if settings.ai_provider == "openai":
            checks.append(("OPENAI_API_KEY", settings.openai_api_key))
        else:
            checks.append(("ANTHROPIC_API_KEY", settings.anthropic_api_key))
    missing = [name for name, value in checks if not value]
    if missing:
        raise SettingsError(f"this command requires {', '.join(missing)} to be set")


def build_broker(settings: Settings) -> AlpacaClient:
    assert settings.alpaca_api_key and settings.alpaca_api_secret
    return AlpacaClient(settings.alpaca_api_key, settings.alpaca_api_secret, settings.execution_mode, settings.broker_timeout_seconds)


def build_gateway(
    settings: Settings, repositories: PersistenceRepositories, broker: AlpacaClient,
    market_data: AlpacaMarketDataProvider, alerts: TelegramAlerter,
) -> ExecutionGateway:
    settlement = SettlementProcessor(repositories, alerts)
    risk_limits = risk_limits_for_profile(settings.risk_profile)
    return ExecutionGateway(repositories, broker, market_data, settlement, alerts, risk_limits, ExecutionMode(settings.execution_mode))


async def run_start(settings: Settings) -> int:
    """`tradepulse start`: the sole operator path to ACTIVE. Refuses
    unconditionally from RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED (run
    reset-risk/reset-integrity first) and from SYSTEM_DEGRADED/MARKET_CLOSED
    (system-derived states this command has no way to safely clear). From
    any other state, proves the broker is actually reachable via a live
    get_account() call before activating -- configured-but-broken
    credentials must never produce ACTIVE."""
    require_credentials(settings, require_ai=False)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    current = await load_session(repositories)
    if current.state in _START_HARD_BLOCKED_STATES:
        logger.error(
            "start_refused",
            extra={
                "event": "start_refused", "state": current.state.value,
                "kill_switch_reason": current.kill_switch_reason, "financial_integrity_reason": current.financial_integrity_reason,
            },
        )
        return 1
    if current.state == SessionState.ACTIVE:
        logger.info("start_noop_already_active", extra={"event": "start_noop_already_active"})
        return 0

    broker = build_broker(settings)
    try:
        await broker.get_account()
    except (AlpacaError, httpx.HTTPError) as exc:
        # AlpacaError covers a definitive HTTP error response; httpx.HTTPError
        # covers everything else (DNS failure, connection refused, timeout)
        # that get_account() doesn't wrap -- broker health being unproven
        # must refuse cleanly either way, never crash with an uncaught
        # traceback or activate on ambiguous connectivity.
        logger.error("start_refused_broker_unreachable", extra={"event": "start_refused_broker_unreachable", "error": str(exc)})
        return 1
    finally:
        await broker.aclose()

    now = datetime.now(UTC)

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state in _START_HARD_BLOCKED_STATES or session.state == SessionState.ACTIVE:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.ACTIVE, True, now)
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="info",
            message=f"{session.state.value} -> active via start", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={"action": "start", "previous_state": session.state.value, "new_state": "active"},
        )
        return new_session, event

    result = await transition_session(repositories, decide)
    if result is None:
        logger.info("start_noop_state_changed_concurrently", extra={"event": "start_noop_state_changed_concurrently"})
        return 0
    logger.info("session_started", extra={"event": "session_started", "previous_state": current.state.value})
    return 0


async def run_stop(settings: Settings) -> int:
    """`tradepulse stop`: always invokable, even with broken or missing
    credentials -- an operator must always be able to halt the runtime."""
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    now = datetime.now(UTC)

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state in _STOP_PRESERVED_STATES:
            return None
        if session.state in (SessionState.DISABLED, SessionState.MANUALLY_STOPPED) and not session.trading_active:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, now)
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="info",
            message=f"{session.state.value} -> manually_stopped via stop", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={"action": "stop", "previous_state": session.state.value, "new_state": "manually_stopped"},
        )
        return new_session, event

    result = await transition_session(repositories, decide)
    if result is None:
        current = await load_session(repositories)
        logger.info(
            "stop_noop",
            extra={
                "event": "stop_noop", "state": current.state.value,
                "kill_switch_reason": current.kill_switch_reason, "financial_integrity_reason": current.financial_integrity_reason,
            },
        )
        return 0
    logger.info("session_stopped", extra={"event": "session_stopped"})
    return 0


async def run_status(settings: Settings) -> int:
    """`tradepulse status`: read-only, always invokable regardless of
    credentials -- reading local session state must never depend on broker
    config being correct."""
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    session = await load_session(repositories)
    logger.info(
        "session_status",
        extra={
            "event": "session_status", "state": session.state.value, "trading_active": session.trading_active,
            "kill_switch_reason": session.kill_switch_reason, "kill_switch_reset_required": session.kill_switch_reset_required,
            "financial_integrity_reason": session.financial_integrity_reason,
            "financial_integrity_manual_reenable_required": session.financial_integrity_manual_reenable_required,
            "updated_at": session.updated_at.isoformat(),
        },
    )
    return 0


async def run_reset_risk(settings: Settings) -> int:
    """`tradepulse reset-risk`: the only way to clear RISK_STOPPED --
    acknowledges the kill-switch and lands on MANUALLY_STOPPED; a separate
    `start` is still required to actually resume trading. Never requires
    credentials -- clears a local flag only, never touches the broker."""
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    now = datetime.now(UTC)

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state != SessionState.RISK_STOPPED:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, now)
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="info",
            message="risk_stopped -> manually_stopped via reset-risk", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={
                "action": "reset_risk", "previous_state": "risk_stopped", "new_state": "manually_stopped",
                "reason": session.kill_switch_reason,
            },
        )
        return new_session, event

    result = await transition_session(repositories, decide)
    if result is None:
        logger.info("reset_risk_noop", extra={"event": "reset_risk_noop"})
        return 0
    logger.info("session_risk_reset", extra={"event": "session_risk_reset"})
    return 0


async def run_reset_integrity(settings: Settings, *, force: bool) -> int:
    """`tradepulse reset-integrity`: the only way to clear
    FINANCIAL_INTEGRITY_BLOCKED. By default runs a real reconciliation pass
    first and requires it to come back clean -- an operator's word alone is
    not evidence that a financial-integrity condition has actually
    resolved. `--force` skips that check for a genuine emergency override,
    but is unmistakably logged as an unverified critical action."""
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    current = await load_session(repositories)
    if current.state != SessionState.FINANCIAL_INTEGRITY_BLOCKED:
        logger.info("reset_integrity_noop", extra={"event": "reset_integrity_noop"})
        return 0

    now = datetime.now(UTC)
    reconciliation_details: dict[str, Any] = {}

    if not force:
        require_credentials(settings, require_ai=False)
        broker = build_broker(settings)
        try:
            alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
            settlement = SettlementProcessor(repositories, alerts)
            summary = await run_reconciliation(repositories, broker, settlement, alerts)
        finally:
            await broker.aclose()
        if summary.status != "ok" or summary.accounting_drift_detected > 0 or summary.missed_fills_detected > 0:
            logger.error(
                "reset_integrity_refused_drift_detected",
                extra={
                    "event": "reset_integrity_refused_drift_detected", "reconciliation_status": summary.status,
                    "accounting_drift_detected": summary.accounting_drift_detected,
                    "missed_fills_detected": summary.missed_fills_detected,
                },
            )
            return 1
        reconciliation_details = {
            "reconciliation_status": summary.status,
            "positions_checked": str(summary.positions_checked),
            "accounting_drift_detected": str(summary.accounting_drift_detected),
            "missed_fills_detected": str(summary.missed_fills_detected),
        }

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state != SessionState.FINANCIAL_INTEGRITY_BLOCKED:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, now)
        if force:
            event = AuditEvent(
                event_id=str(uuid4()), event_type="session_transition", severity="critical",
                message="financial_integrity_blocked force-cleared WITHOUT a verifying reconciliation pass", occurred_at=now,
                entity_type="trading_session", entity_id=SESSION_RECORD_ID,
                details={
                    "action": "reset_integrity_forced", "previous_state": "financial_integrity_blocked",
                    "new_state": "manually_stopped", "reason": session.financial_integrity_reason,
                },
            )
        else:
            event = AuditEvent(
                event_id=str(uuid4()), event_type="session_transition", severity="info",
                message="financial_integrity_blocked cleared after a clean reconciliation pass", occurred_at=now,
                entity_type="trading_session", entity_id=SESSION_RECORD_ID,
                details={
                    "action": "reset_integrity", "previous_state": "financial_integrity_blocked",
                    "new_state": "manually_stopped", "reason": session.financial_integrity_reason,
                    **reconciliation_details,
                },
            )
        return new_session, event

    result = await transition_session(repositories, decide)
    if result is None:
        logger.info("reset_integrity_noop_state_changed_concurrently", extra={"event": "reset_integrity_noop_state_changed_concurrently"})
        return 0
    logger.info("session_integrity_reset", extra={"event": "session_integrity_reset", "forced": force})
    return 0
