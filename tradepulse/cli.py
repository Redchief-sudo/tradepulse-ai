"""CLI entrypoint -- the sole way into this runtime, invoked externally on a
schedule (cron, systemd timer, etc.). No command runs its own internal
scheduling loop: each invocation does its work once and exits, matching the
CLI-driven, cron-external architecture decided for this system (see docs/).

`tradepulse scan` runs AI-driven candidate discovery and the stop/target
position monitor CONCURRENTLY (not sequentially) in one process, sharing a
composition root but each independently lock-protected -- position
protection latency is never tied to how long the AI discovery call takes.
`tradepulse monitor`, `tradepulse settle`, and `tradepulse reconcile` are also
available standalone for operators who want a different cadence than the
scan cycle's (position protection is typically wanted more often than
discovery; settlement retries want a short, independent cadence so a quiet
market doesn't leave one stranded; reconciliation is an after-the-fact audit
pass, typically wanted less often). Example crontab:

    */15 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan      >> /var/log/tradepulse.log 2>&1
    */2  9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse monitor   >> /var/log/tradepulse.log 2>&1
    *    *    * * *    cd /path/to/repo && .venv/bin/tradepulse settle    >> /var/log/tradepulse.log 2>&1
    0    */6  * * *    cd /path/to/repo && .venv/bin/tradepulse reconcile >> /var/log/tradepulse.log 2>&1

An overlapping invocation of the SAME command (cron re-firing before a slow
run finishes) is handled at the application level via a database-enforced
lease per command (see persistence/lock.py) -- it does not depend on the
operator's cron/flock configuration being correct. A caller that can't
acquire its lease exits cleanly (status 0, logged as skipped), not as a
failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from typing import Any
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.config import Settings, SettingsError, default_strategy_weights, risk_limits_for_profile
from tradepulse.config.logging import configure_logging
from tradepulse.execution import ExecutionGateway
from tradepulse.models import AuditEvent, ExecutionMode, SessionState, TradingSession
from tradepulse.monitor import MonitorCycleSummary, run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, release_lock
from tradepulse.providers import AIProvider, AlpacaMarketDataProvider, AnthropicAIProvider, OpenAIProvider
from tradepulse.reconciliation import run_reconciliation
from tradepulse.risk import SESSION_RECORD_ID, load_session, transition_session
from tradepulse.scanner import ScanCycleSummary, run_scan_cycle
from tradepulse.settlement import SettlementProcessor
from tradepulse.strategy import load_executable_universe

logger = logging.getLogger(__name__)

SCAN_LOCK_KEY = "scan"
MONITOR_LOCK_KEY = "monitor"
SETTLE_LOCK_KEY = "settle"
RECONCILE_LOCK_KEY = "reconcile"
# States `start` refuses unconditionally: RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED
# need their own explicit reset-risk/reset-integrity command first;
# SYSTEM_DEGRADED/MARKET_CLOSED are system-derived states this command has
# no way to safely verify have actually cleared (see cli.py's session
# commands section below).
_START_HARD_BLOCKED_STATES = frozenset(
    {SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED, SessionState.SYSTEM_DEGRADED, SessionState.MARKET_CLOSED}
)
# `stop` must never downgrade an active safety block into a plain
# MANUALLY_STOPPED -- that would erase the reason and the reset
# requirement, letting a bare `start` through even though nothing was reset.
_STOP_PRESERVED_STATES = frozenset({SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED})
# Generous ceilings above any realistic single run -- long enough to never
# steal a live run's lease, short enough that a crashed process doesn't
# block that command indefinitely. Monitor's and settle's are shorter since
# they're meant to be cron'd more frequently than scan/reconcile.
SCAN_LOCK_TTL_SECONDS = 600
MONITOR_LOCK_TTL_SECONDS = 300
SETTLE_LOCK_TTL_SECONDS = 300
RECONCILE_LOCK_TTL_SECONDS = 600


def _require_credentials(settings: Settings, *, require_ai: bool) -> None:
    checks = [("ALPACA_API_KEY", settings.alpaca_api_key), ("ALPACA_API_SECRET", settings.alpaca_api_secret)]
    if require_ai:
        if settings.ai_provider == "openai":
            checks.append(("OPENAI_API_KEY", settings.openai_api_key))
        else:
            checks.append(("ANTHROPIC_API_KEY", settings.anthropic_api_key))
    missing = [name for name, value in checks if not value]
    if missing:
        raise SettingsError(f"this command requires {', '.join(missing)} to be set")


def _build_broker(settings: Settings) -> AlpacaClient:
    assert settings.alpaca_api_key and settings.alpaca_api_secret
    return AlpacaClient(settings.alpaca_api_key, settings.alpaca_api_secret, settings.execution_mode, settings.broker_timeout_seconds)


def _build_ai_provider(settings: Settings) -> AIProvider:
    if settings.ai_provider == "openai":
        assert settings.openai_api_key
        return OpenAIProvider(settings.openai_api_key, settings.openai_model, settings.ai_timeout_seconds, settings.openai_base_url)
    assert settings.anthropic_api_key
    return AnthropicAIProvider(settings.anthropic_api_key, settings.anthropic_model, settings.ai_timeout_seconds, settings.anthropic_base_url)


def _build_gateway(
    settings: Settings, repositories: PersistenceRepositories, broker: AlpacaClient,
    market_data: AlpacaMarketDataProvider, alerts: TelegramAlerter,
) -> ExecutionGateway:
    settlement = SettlementProcessor(repositories, alerts)
    risk_limits = risk_limits_for_profile(settings.risk_profile)
    return ExecutionGateway(repositories, broker, market_data, settlement, alerts, risk_limits, ExecutionMode(settings.execution_mode))


async def _run_scan_leg(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, ai_provider: AIProvider,
    market_data: AlpacaMarketDataProvider, broker: AlpacaClient, gateway: ExecutionGateway,
    settings: Settings,
) -> ScanCycleSummary | None:
    owner_token = str(uuid4())
    if not await acquire_lock(database, SCAN_LOCK_KEY, owner_token, "scan", SCAN_LOCK_TTL_SECONDS):
        logger.info("scan_skipped_lock_held", extra={"event": "scan_skipped_lock_held"})
        return None
    try:
        universe = load_executable_universe(settings)
        risk_limits = risk_limits_for_profile(settings.risk_profile)
        strategy_weights = default_strategy_weights(datetime.now(UTC))
        return await run_scan_cycle(
            repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, strategy_weights=strategy_weights
        )
    finally:
        await release_lock(database, SCAN_LOCK_KEY, owner_token)


async def _run_monitor_leg(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, broker: AlpacaClient,
    gateway: ExecutionGateway, alerts: TelegramAlerter,
) -> MonitorCycleSummary | None:
    owner_token = str(uuid4())
    if not await acquire_lock(database, MONITOR_LOCK_KEY, owner_token, "monitor", MONITOR_LOCK_TTL_SECONDS):
        logger.info("monitor_skipped_lock_held", extra={"event": "monitor_skipped_lock_held"})
        return None
    try:
        return await run_position_monitor(repositories, broker, gateway, alerts)
    finally:
        await release_lock(database, MONITOR_LOCK_KEY, owner_token)


def _log_scan_result(result: ScanCycleSummary | BaseException | None) -> bool:
    """Returns True if this leg should fail the process's exit code."""
    if result is None:
        return False
    if isinstance(result, BaseException):
        logger.error("scan_cycle_crashed", extra={"event": "scan_cycle_crashed", "error": str(result)})
        return True
    logger.info(
        "scan_cycle_finished",
        extra={
            "event": "scan_cycle_finished", "scan_generation": result.scan_run_id, "status": result.status.value,
            "candidates_discovered": result.candidates_discovered, "candidates_approved": result.candidates_approved,
            "orders_submitted": result.orders_submitted,
        },
    )
    if result.error:
        logger.error("scan_cycle_failed", extra={"event": "scan_cycle_failed", "error": result.error})
        return True
    return False


def _log_monitor_result(result: MonitorCycleSummary | BaseException | None) -> bool:
    if result is None:
        return False
    if isinstance(result, BaseException):
        logger.error("monitor_cycle_crashed", extra={"event": "monitor_cycle_crashed", "error": str(result)})
        return True
    logger.info(
        "monitor_cycle_finished",
        extra={
            "event": "monitor_cycle_finished", "status": result.status,
            "positions_checked": result.positions_checked, "exits_triggered": result.exits_triggered,
        },
    )
    if result.status == "degraded":
        logger.error("monitor_cycle_degraded", extra={"event": "monitor_cycle_degraded", "error": result.error})
        return True
    return False


async def _run_scan(settings: Settings) -> int:
    """`tradepulse scan`: discovery and position protection, concurrently."""
    _require_credentials(settings, require_ai=True)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    broker = _build_broker(settings)
    ai_provider = _build_ai_provider(settings)
    try:
        market_data = AlpacaMarketDataProvider(broker)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        gateway = _build_gateway(settings, repositories, broker, market_data, alerts)

        scan_result, monitor_result = await asyncio.gather(
            _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, settings),
            _run_monitor_leg(database, repositories, broker, gateway, alerts),
            return_exceptions=True,
        )
    finally:
        await broker.aclose()
        await ai_provider.aclose()

    scan_failed = _log_scan_result(scan_result)
    monitor_failed = _log_monitor_result(monitor_result)
    return 1 if (scan_failed or monitor_failed) else 0


async def _run_monitor(settings: Settings) -> int:
    """`tradepulse monitor`: standalone, for a tighter cadence than scan's."""
    _require_credentials(settings, require_ai=False)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    broker = _build_broker(settings)
    try:
        market_data = AlpacaMarketDataProvider(broker)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        gateway = _build_gateway(settings, repositories, broker, market_data, alerts)
        result = await _run_monitor_leg(database, repositories, broker, gateway, alerts)
    finally:
        await broker.aclose()

    return 1 if _log_monitor_result(result) else 0


async def _run_settle(settings: Settings) -> int:
    """`tradepulse settle`: independently drains any due settlement retry --
    the only production caller otherwise is the gateway's own post-fill hook,
    which never fires if trading goes quiet while a retry is still pending."""
    _require_credentials(settings, require_ai=False)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
    settlement = SettlementProcessor(repositories, alerts)
    owner_token = str(uuid4())
    if not await acquire_lock(database, SETTLE_LOCK_KEY, owner_token, "settle", SETTLE_LOCK_TTL_SECONDS):
        logger.info("settle_skipped_lock_held", extra={"event": "settle_skipped_lock_held"})
        return 0
    try:
        summary = await settlement.process_pending()
    finally:
        await release_lock(database, SETTLE_LOCK_KEY, owner_token)

    logger.info(
        "settle_finished",
        extra={
            "event": "settle_finished", "ok": summary.ok, "processed": summary.processed,
            "completed": summary.completed, "retryable_failed": summary.retryable_failed,
            "terminal_failed": summary.terminal_failed, "integrity_blocked": summary.integrity_blocked,
        },
    )
    return 0 if summary.ok else 1


async def _run_reconcile(settings: Settings) -> int:
    """`tradepulse reconcile`: after-the-fact audit against Alpaca's real state."""
    _require_credentials(settings, require_ai=False)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    broker = _build_broker(settings)
    owner_token = str(uuid4())
    try:
        if not await acquire_lock(database, RECONCILE_LOCK_KEY, owner_token, "reconcile", RECONCILE_LOCK_TTL_SECONDS):
            logger.info("reconcile_skipped_lock_held", extra={"event": "reconcile_skipped_lock_held"})
            return 0
        try:
            alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
            settlement = SettlementProcessor(repositories, alerts)
            summary = await run_reconciliation(repositories, broker, settlement, alerts)
        finally:
            await release_lock(database, RECONCILE_LOCK_KEY, owner_token)
    finally:
        await broker.aclose()

    logger.info(
        "reconciliation_finished",
        extra={
            "event": "reconciliation_finished", "status": summary.status,
            "positions_checked": summary.positions_checked, "view_drift_corrected": summary.view_drift_corrected,
            "accounting_drift_detected": summary.accounting_drift_detected, "fills_checked": summary.fills_checked,
            "missed_fills_detected": summary.missed_fills_detected, "late_fills_recovered": summary.late_fills_recovered,
        },
    )
    if summary.status == "degraded":
        logger.error("reconciliation_degraded", extra={"event": "reconciliation_degraded", "error": summary.error})
        return 1
    return 0


async def _run_start(settings: Settings) -> int:
    """`tradepulse start`: the sole operator path to ACTIVE. Refuses
    unconditionally from RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED (run
    reset-risk/reset-integrity first) and from SYSTEM_DEGRADED/MARKET_CLOSED
    (system-derived states this command has no way to safely clear). From
    any other state, proves the broker is actually reachable via a live
    get_account() call before activating -- configured-but-broken
    credentials must never produce ACTIVE."""
    _require_credentials(settings, require_ai=False)

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

    broker = _build_broker(settings)
    try:
        await broker.get_account()
    except AlpacaError as exc:
        logger.error("start_refused_broker_unreachable", extra={"event": "start_refused_broker_unreachable", "error": exc.message})
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


async def _run_stop(settings: Settings) -> int:
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


async def _run_status(settings: Settings) -> int:
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


async def _run_reset_risk(settings: Settings) -> int:
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


async def _run_reset_integrity(settings: Settings, *, force: bool) -> int:
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
        _require_credentials(settings, require_ai=False)
        broker = _build_broker(settings)
        try:
            alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
            settlement = SettlementProcessor(repositories, alerts)
            summary = await run_reconciliation(repositories, broker, settlement, alerts)
        finally:
            await broker.aclose()
        if summary.status != "ok" or summary.accounting_drift_detected > 0:
            logger.error(
                "reset_integrity_refused_drift_detected",
                extra={
                    "event": "reset_integrity_refused_drift_detected", "reconciliation_status": summary.status,
                    "accounting_drift_detected": summary.accounting_drift_detected,
                },
            )
            return 1
        reconciliation_details = {
            "reconciliation_status": summary.status,
            "positions_checked": str(summary.positions_checked),
            "accounting_drift_detected": str(summary.accounting_drift_detected),
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradepulse", description="TradePulse AI trading runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="run one AI-driven scan cycle and the position monitor concurrently, then exit")
    subparsers.add_parser("monitor", help="run one stop/target position-protection pass and exit")
    subparsers.add_parser("settle", help="drain any due settlement retries and exit")
    subparsers.add_parser("reconcile", help="run one reconciliation pass against Alpaca's real state and exit")
    subparsers.add_parser("start", help="activate the trading session (refuses from RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED/SYSTEM_DEGRADED/MARKET_CLOSED)")
    subparsers.add_parser("stop", help="deactivate the trading session (never downgrades an active safety block)")
    subparsers.add_parser("status", help="report the current trading session state and exit")
    subparsers.add_parser("reset-risk", help="acknowledge and clear a RISK_STOPPED kill-switch (run start afterward to resume trading)")
    reset_integrity_parser = subparsers.add_parser(
        "reset-integrity", help="clear a FINANCIAL_INTEGRITY_BLOCKED session after a clean reconciliation pass (run start afterward to resume trading)"
    )
    reset_integrity_parser.add_argument(
        "--force", action="store_true", help="skip the verifying reconciliation pass (emergency override, logged as a critical unverified action)"
    )
    return parser


_COMMANDS: dict[str, Any] = {
    "scan": _run_scan, "monitor": _run_monitor, "settle": _run_settle, "reconcile": _run_reconcile,
    "start": _run_start, "stop": _run_stop, "status": _run_status, "reset-risk": _run_reset_risk,
}


def _load_dotenv(path: Path = Path(".env")) -> None:
    """KEY=VALUE lines, '#' comments and blanks skipped. Real environment
    variables always win -- this only fills in values not already set, so
    `ANTHROPIC_API_KEY=... tradepulse scan` still overrides the file."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            environ.setdefault(key, value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _load_dotenv()
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"tradepulse: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings.log_level)

    try:
        if args.command == "reset-integrity":
            return asyncio.run(_run_reset_integrity(settings, force=args.force))
        return asyncio.run(_COMMANDS[args.command](settings))
    except SettingsError as exc:
        print(f"tradepulse: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
