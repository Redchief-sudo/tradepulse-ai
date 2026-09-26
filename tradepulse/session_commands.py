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
from uuid import uuid4

import httpx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.config import Settings, SettingsError, risk_limits_for_profile
from tradepulse.execution import ExecutionGateway
from tradepulse.models import AuditEvent, ExecutionMode, ReconciliationOutcome, SessionState, TradingSession
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
    """Clear only a deliberately acknowledged, independently verified block.

    Reconciliation may repair derived accounting. A fresh valuation and exact
    checkpoint proof must then remain valid through the session-write commit.
    No override may erase unresolved financial evidence.
    """
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    current = await load_session(repositories)
    if current.state != SessionState.FINANCIAL_INTEGRITY_BLOCKED:
        logger.info("reset_integrity_noop", extra={"event": "reset_integrity_noop"})
        return 0
    if force:
        logger.error("reset_integrity_force_refused", extra={"event": "reset_integrity_force_refused",
                     "reason": "independent accounting verification is mandatory"})
        return 1

    from tradepulse.persistence.codec import encode_payload
    from tradepulse.reconciliation.epochs import require_unlock_proof, unlock_proof
    from tradepulse.time import aware_utc
    from tradepulse.valuation import marked_snapshot, record_valuation, reconciliation_outcome, valuation_record

    broker = None
    try:
        require_credentials(settings, require_ai=False)
        broker = build_broker(settings)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        settlement = SettlementProcessor(repositories, alerts)
        summary = await run_reconciliation(repositories, broker, settlement, alerts)
        if summary.status != "ok" or summary.accounting_drift_detected or summary.missed_fills_detected:
            logger.error("reset_integrity_refused_drift_detected", extra={
                "event": "reset_integrity_refused_drift_detected", "reconciliation_status": summary.status,
                "accounting_drift_detected": summary.accounting_drift_detected,
                "missed_fills_detected": summary.missed_fills_detected,
            })
            return 1
        # Capture BEFORE external observations and multi-read valuation. A
        # concurrent accounting change during either must invalidate the proof.
        proof = await database.run(unlock_proof)
        if proof["issues"]:
            logger.error("reset_integrity_refused_checkpoint_unproven", extra={
                "event": "reset_integrity_refused_checkpoint_unproven", "issues": proof["issues"],
            })
            return 1
        account = await broker.get_account()
        positions = await broker.get_positions()
        if proof['account_identity_digest'] is not None:
            from tradepulse.verification.integrity import canonical, digest
            identity = {'account_id': account.account_id, 'account_number': account.account_number}
            if digest(canonical(identity)) != proof['account_identity_digest']:
                raise ValueError('RESET_BROKER_ACCOUNT_IDENTITY_CHANGED')
        aware_utc(account.received_at, field_name="reset_account_received_at")
        for position in positions:
            aware_utc(position.received_at, field_name="reset_position_received_at")
        snapshot = await marked_snapshot(repositories, account, positions)
        if reconciliation_outcome(snapshot) != ReconciliationOutcome.MATCHED:
            await record_valuation(repositories, snapshot)
            logger.error("reset_integrity_refused_current_accounting_unproven", extra={
                "event": "reset_integrity_refused_current_accounting_unproven",
                "outcome": reconciliation_outcome(snapshot).value,
            })
            return 1
        receipt = valuation_record(snapshot)
    except Exception as exc:  # noqa: BLE001 - evidence/provider failure must preserve the latch
        logger.error("reset_integrity_verification_failed", extra={
            "event": "reset_integrity_verification_failed", "error": str(exc),
        })
        return 1
    finally:
        if broker is not None:
            await broker.aclose()

    now = datetime.now(UTC)

    def validate_write(connection):
        # This runs in transition_session's BEGIN IMMEDIATE transaction, before
        # any reset write. Never delete holds: every hold must already be absent.
        require_unlock_proof(connection, proof["digest"])
        connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (receipt.record_id, encode_payload(receipt), now.isoformat()))

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state != SessionState.FINANCIAL_INTEGRITY_BLOCKED:
            return None
        new_session = TradingSession(SESSION_RECORD_ID, SessionState.MANUALLY_STOPPED, False, now)
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="info",
            message="financial_integrity_blocked cleared after independently verified accounting", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={
                "action": "reset_integrity", "previous_state": "financial_integrity_blocked",
                "new_state": "manually_stopped", "reason": session.financial_integrity_reason,
                "reconciliation_status": summary.status,
                "positions_checked": str(summary.positions_checked),
                "accounting_drift_detected": str(summary.accounting_drift_detected),
                "missed_fills_detected": str(summary.missed_fills_detected),
                "equity_reconciliation_status": snapshot.equity_reconciliation_status,
                "valuation_record_id": receipt.record_id, "financial_evidence_sha256": proof["digest"],
            },
        )
        return new_session, event

    try:
        result = await transition_session(repositories, decide, validate_write=validate_write)
    except Exception as exc:  # noqa: BLE001 - atomic rollback preserves all evidence and the latch
        logger.error("reset_integrity_commit_refused", extra={"event": "reset_integrity_commit_refused", "error": str(exc)})
        return 1
    if result is None:
        logger.error("reset_integrity_state_changed_concurrently", extra={"event": "reset_integrity_state_changed_concurrently"})
        return 1
    logger.info("session_integrity_reset", extra={"event": "session_integrity_reset", "forced": False})
    return 0
