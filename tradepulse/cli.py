"""CLI entrypoint -- the sole way into this runtime, invoked externally on a
schedule (cron, systemd timer, etc.). No command runs its own internal
scheduling loop: each invocation does its work once and exits, matching the
CLI-driven, cron-external architecture decided for this system (see docs/).

`tradepulse scan --asset-class=<equity|crypto|option> [more...]` runs
AI-driven candidate discovery for one OR MORE lanes plus the stop/target
position monitor, all CONCURRENTLY (not sequentially) in one process,
sharing a composition root but each independently lock-protected --
position protection latency is never tied to how long any lane's AI
discovery call takes, and a slow or crashing lane never blocks its siblings
(asyncio.gather(..., return_exceptions=True), never TaskGroup -- see
_run_scan). Each lane is independently scheduled and independently
lock-protected (separate lock keys, see scan_lock_key below) so a tighter
crypto cadence never re-scans, or re-bills AI calls for, equities that are
market-closed, and vice versa; every lane shares the SAME risk engine,
execution gateway, and settlement/reconciliation pipeline -- lane
separation is discovery-only. This is the "parallel multi-asset
supervisor": several specialized market-discovery lanes running
concurrently, one centralized capital/risk authority underneath all of
them, never N independent trading bots. `tradepulse monitor`,
`tradepulse settle`, and `tradepulse reconcile` are also available
standalone for operators who want a different cadence than the scan
cycle's (position protection is typically wanted more often than
discovery; settlement retries want a short, independent cadence so a quiet
market doesn't leave one stranded; reconciliation is an after-the-fact audit
pass, typically wanted less often). Example crontab (one lane per line, or
combine any subset into a single `--asset-class` invocation):

    */15 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=equity  >> /var/log/tradepulse.log 2>&1
    */10 *    * * *    cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=crypto  >> /var/log/tradepulse.log 2>&1
    */20 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan --asset-class=option   >> /var/log/tradepulse.log 2>&1
    */2  9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse monitor   >> /var/log/tradepulse.log 2>&1
    *    *    * * *    cd /path/to/repo && .venv/bin/tradepulse settle    >> /var/log/tradepulse.log 2>&1
    0    */6  * * *    cd /path/to/repo && .venv/bin/tradepulse reconcile >> /var/log/tradepulse.log 2>&1

An overlapping invocation of the SAME command (cron re-firing before a slow
run finishes) is handled at the application level via a database-enforced
lease per command (see persistence/lock.py) -- it does not depend on the
operator's cron/flock configuration being correct. A caller that can't
acquire its lease exits cleanly (status 0, logged as skipped), not as a
failure.

`tradepulse run` is the ONE deliberate, explicit exception to "no internal
scheduling loop" above -- not a repeal of it. It's the normal interactive
startup: resolves capabilities, opens the local dashboard, activates the
session, then keeps independent equity/crypto/option scan lanes, the
position monitor, and settlement cycling concurrently (one asyncio task
per lane, genuinely parallel -- never a shared serial loop) until Ctrl+C.
`scan`/`monitor`/`settle`/`reconcile` remain one-shot and cron-external;
`run` just re-invokes their exact same per-cycle leg functions on a timer
instead of a different execution path. See _run_application.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.config import Settings, SettingsError, default_strategy_weights, risk_limits_for_profile
from tradepulse.config.logging import configure_logging
from tradepulse.execution import ExecutionGateway
from tradepulse.models import AssetClass, AuditEvent, SessionState
from tradepulse.monitor import MonitorCycleSummary, run_position_monitor
from tradepulse.persistence import (
    AsyncSQLiteDatabase,
    PersistenceRepositories,
    acquire_lock,
    release_lock,
    run_with_lock_renewal,
)
from tradepulse.providers import (
    AIProvider,
    AlpacaMarketDataProvider,
    AnthropicAIProvider,
    MarketDataCapabilities,
    MarketDataCapabilityError,
    OpenAIProvider,
    resolve_market_data_capabilities,
)
from tradepulse.reconciliation import run_reconciliation
from tradepulse.risk import load_session
from tradepulse.scanner import ScanCycleSummary, run_scan_cycle
from tradepulse.session_commands import (
    build_broker as _build_broker,
    build_gateway as _build_gateway,
    require_credentials as _require_credentials,
    run_reset_integrity as _run_reset_integrity,
    run_reset_risk as _run_reset_risk,
    run_start as _run_start,
    run_status as _run_status,
    run_stop as _run_stop,
)
from tradepulse.settlement import SettlementBatchSummary, SettlementProcessor
from tradepulse.strategy import load_executable_universe

logger = logging.getLogger(__name__)

SCAN_LOCK_KEY = "scan"
MONITOR_LOCK_KEY = "monitor"
SETTLE_LOCK_KEY = "settle"
RECONCILE_LOCK_KEY = "reconcile"
# Generous ceilings above any realistic single run -- long enough to never
# steal a live run's lease, short enough that a crashed process doesn't
# block that command indefinitely. Monitor's and settle's are shorter since
# they're meant to be cron'd more frequently than scan/reconcile.
SCAN_LOCK_TTL_SECONDS = 600
MONITOR_LOCK_TTL_SECONDS = 300
SETTLE_LOCK_TTL_SECONDS = 300
RECONCILE_LOCK_TTL_SECONDS = 600

# `tradepulse run` -- the whole supervisor's lifetime lease, distinct from
# every per-cycle lock above (see _run_application). Cadences below match
# the README's own crontab example exactly -- not configurable in this pass.
RUN_LOCK_KEY = "run"
RUN_LOCK_TTL_SECONDS = 60
EQUITY_SCAN_INTERVAL_SECONDS = 900
CRYPTO_SCAN_INTERVAL_SECONDS = 600
OPTION_SCAN_INTERVAL_SECONDS = 1200
MONITOR_INTERVAL_SECONDS = 120
SETTLE_INTERVAL_SECONDS = 60
# Bounded retry after an INDETERMINATE market-clock check (broker/network
# trouble) -- distinct from a CONFIRMED-closed result, which correctly
# waits the full lane interval instead (see _scan_action). Short enough to
# recover quickly from a transient hiccup, long enough to never hammer.
MARKET_CLOCK_RETRY_SECONDS = 30
BROWSER_OPEN_MAX_WAIT_SECONDS = 5.0
BROWSER_OPEN_POLL_SECONDS = 0.2


def scan_lock_key(asset_class: AssetClass) -> str:
    """Per-lane, not one global scan lock -- a slow AI/market-data call in
    one asset-class lane must never block the other's cron-driven
    invocation. See scanner/coordinator.py::run_scan_cycle."""
    return f"{SCAN_LOCK_KEY}:{asset_class.value}"


def _build_ai_provider(settings: Settings) -> AIProvider:
    if settings.ai_provider == "openai":
        assert settings.openai_api_key
        return OpenAIProvider(settings.openai_api_key, settings.openai_model, settings.ai_timeout_seconds, settings.openai_base_url)
    assert settings.anthropic_api_key
    return AnthropicAIProvider(settings.anthropic_api_key, settings.anthropic_model, settings.ai_timeout_seconds, settings.anthropic_base_url)


async def _resolve_and_apply_market_data_feeds(broker: AlpacaClient, settings: Settings) -> MarketDataCapabilities:
    """Resolves which Alpaca feeds this account is entitled to (see
    providers/market_data_capability.py) and applies them to `broker`
    ONCE, before any market-data work starts -- called from both
    `_run_scan` and `_run_monitor` (the only two commands that fetch
    quotes, directly or via the gateway's authoritative quote fetch for
    protective exits), never per-lane. Any resolution failure -- an
    explicit algo_trader_plus requirement not met, or an indeterminate
    probe outcome (auth/rate-limit/transport/malformed-response) -- is
    normalized to MarketDataCapabilityError so main() has exactly one
    clean, expected exception type to report for this whole class of
    startup problem. Returns the resolved capabilities so callers can stamp
    them onto whatever they persist (see ScanRun.market_data_tier/
    equity_feed/option_feed) -- a durable record of what feed was actually
    used, not something a later reader has to re-probe Alpaca to learn."""
    try:
        capabilities = await resolve_market_data_capabilities(broker, settings.alpaca_market_data_tier)
    except MarketDataCapabilityError:
        raise
    except (AlpacaError, httpx.HTTPError) as exc:
        raise MarketDataCapabilityError(f"failed to resolve Alpaca market-data capabilities: {exc}") from exc
    broker.set_market_data_feeds(equity_feed=capabilities.equity_feed, option_feed=capabilities.option_feed)
    logger.info(
        "market_data_capabilities_resolved",
        extra={
            "event": "market_data_capabilities_resolved", "tier": capabilities.tier_label,
            "equity_feed": capabilities.equity_feed, "option_feed": capabilities.option_feed,
        },
    )
    return capabilities


def _lease_lost_signal(
    alerts: TelegramAlerter, lock_key: str, owner_token: str,
) -> tuple[asyncio.Event, Callable[[], Awaitable[None]]]:
    """Builds the (event, callback) pair every renewable command lease uses:
    the event lets the coordinator's own loop stop starting new work once
    exclusivity may be gone, while in-flight work (a broker order being
    polled, a settlement mid-write) still finishes -- aborting that is more
    dangerous than letting it complete. The callback both flips the event
    and alerts an operator."""
    lease_lost = asyncio.Event()

    async def on_lease_lost() -> None:
        lease_lost.set()
        await alerts.send(
            "critical", f"Lock renewal failed for '{lock_key}' -- lease may have been reclaimed by a concurrent run.",
            {"lock_key": lock_key, "owner_token": owner_token},
        )

    return lease_lost, on_lease_lost


async def _run_scan_leg(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, ai_provider: AIProvider,
    market_data: AlpacaMarketDataProvider, broker: AlpacaClient, gateway: ExecutionGateway,
    settings: Settings, alerts: TelegramAlerter, asset_class: AssetClass,
    capabilities: MarketDataCapabilities | None = None,
) -> ScanCycleSummary | None:
    lock_key = scan_lock_key(asset_class)
    owner_token = str(uuid4())
    if not await acquire_lock(database, lock_key, owner_token, "scan", SCAN_LOCK_TTL_SECONDS):
        logger.info("scan_skipped_lock_held", extra={"event": "scan_skipped_lock_held", "asset_class": asset_class.value})
        return None
    try:
        universe = load_executable_universe(settings)
        risk_limits = risk_limits_for_profile(settings.risk_profile)
        strategy_weights = default_strategy_weights(datetime.now(UTC))
        lease_lost, on_lease_lost = _lease_lost_signal(alerts, lock_key, owner_token)
        return await run_with_lock_renewal(
            database, lock_key, owner_token, SCAN_LOCK_TTL_SECONDS,
            run_scan_cycle(
                repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class,
                strategy_weights=strategy_weights, lease_lost=lease_lost, capabilities=capabilities,
            ),
            on_renewal_failed=on_lease_lost,
        )
    finally:
        await release_lock(database, lock_key, owner_token)


async def _run_monitor_leg(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, broker: AlpacaClient,
    gateway: ExecutionGateway, alerts: TelegramAlerter, settings: Settings,
) -> MonitorCycleSummary | None:
    owner_token = str(uuid4())
    if not await acquire_lock(database, MONITOR_LOCK_KEY, owner_token, "monitor", MONITOR_LOCK_TTL_SECONDS):
        logger.info("monitor_skipped_lock_held", extra={"event": "monitor_skipped_lock_held"})
        return None
    try:
        lease_lost, on_lease_lost = _lease_lost_signal(alerts, MONITOR_LOCK_KEY, owner_token)
        risk_limits = risk_limits_for_profile(settings.risk_profile)
        return await run_with_lock_renewal(
            database, MONITOR_LOCK_KEY, owner_token, MONITOR_LOCK_TTL_SECONDS,
            run_position_monitor(repositories, broker, gateway, alerts, risk_limits, lease_lost=lease_lost),
            on_renewal_failed=on_lease_lost,
        )
    finally:
        await release_lock(database, MONITOR_LOCK_KEY, owner_token)


def _log_scan_result(result: ScanCycleSummary | BaseException | None, asset_class: AssetClass) -> bool:
    """Returns True if this leg should fail the process's exit code."""
    if result is None:
        return False
    if isinstance(result, BaseException):
        logger.error("scan_cycle_crashed", extra={"event": "scan_cycle_crashed", "error": str(result), "asset_class": asset_class.value})
        return True
    logger.info(
        "scan_cycle_finished",
        extra={
            "event": "scan_cycle_finished", "scan_generation": result.scan_run_id, "status": result.status.value,
            "asset_class": asset_class.value,
            "candidates_discovered": result.candidates_discovered, "candidates_approved": result.candidates_approved,
            "orders_submitted": result.orders_submitted,
        },
    )
    if result.error:
        logger.error("scan_cycle_failed", extra={"event": "scan_cycle_failed", "error": result.error, "asset_class": asset_class.value})
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


async def _run_scan(settings: Settings, asset_classes: list[AssetClass]) -> int:
    """`tradepulse scan --asset-class=... [asset-class ...]`: one or more
    lanes' discovery plus the stop/target position monitor, all CONCURRENTLY
    (not sequentially) in one process. Each lane is independently
    lock-protected (see scan_lock_key) and independently attributed (see
    ScanRun.asset_class), so a slow/crashing lane never blocks or takes down
    its siblings -- return_exceptions=True on the single gather call below
    covers every leg, scan or monitor alike (deliberately NOT
    asyncio.TaskGroup, which cancels every sibling task on one's first
    exception -- exactly wrong here). A single `--asset-class` value behaves
    identically to before this generalization -- this is purely a wider
    fan-out over the same per-lane unit (_run_scan_leg), not a rewrite of
    it. Monitor is asset-class-agnostic (protects every open position
    regardless of lane) and stays bundled in exactly as before -- a second
    lane's concurrent monitor invocation cleanly no-ops via
    MONITOR_LOCK_KEY's own lock-held skip."""
    _require_credentials(settings, require_ai=True)

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    broker = _build_broker(settings)
    ai_provider = _build_ai_provider(settings)
    try:
        capabilities = await _resolve_and_apply_market_data_feeds(broker, settings)

        market_data = AlpacaMarketDataProvider(broker)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        gateway = _build_gateway(settings, repositories, broker, market_data, alerts)

        scan_legs = [
            _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, settings, alerts, asset_class, capabilities)
            for asset_class in asset_classes
        ]
        *scan_results, monitor_result = await asyncio.gather(
            *scan_legs,
            _run_monitor_leg(database, repositories, broker, gateway, alerts, settings),
            return_exceptions=True,
        )
    finally:
        await broker.aclose()
        await ai_provider.aclose()

    scan_failed = any(
        _log_scan_result(result, asset_class) for result, asset_class in zip(scan_results, asset_classes, strict=True)
    )
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
        await _resolve_and_apply_market_data_feeds(broker, settings)

        market_data = AlpacaMarketDataProvider(broker)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        gateway = _build_gateway(settings, repositories, broker, market_data, alerts)
        result = await _run_monitor_leg(database, repositories, broker, gateway, alerts, settings)
    finally:
        await broker.aclose()

    return 1 if _log_monitor_result(result) else 0


async def _run_settle_leg(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, settlement: SettlementProcessor,
    alerts: TelegramAlerter,
) -> SettlementBatchSummary | None:
    """The exact acquire/renew/release logic `tradepulse settle` runs once --
    extracted so a supervised settle task under `tradepulse run` (see
    _settle_action) and the standalone command call ONE function, never two
    copies of this lock plumbing."""
    owner_token = str(uuid4())
    if not await acquire_lock(database, SETTLE_LOCK_KEY, owner_token, "settle", SETTLE_LOCK_TTL_SECONDS):
        logger.info("settle_skipped_lock_held", extra={"event": "settle_skipped_lock_held"})
        return None
    try:
        lease_lost, on_lease_lost = _lease_lost_signal(alerts, SETTLE_LOCK_KEY, owner_token)
        return await run_with_lock_renewal(
            database, SETTLE_LOCK_KEY, owner_token, SETTLE_LOCK_TTL_SECONDS,
            settlement.process_pending(lease_lost=lease_lost),
            on_renewal_failed=on_lease_lost,
        )
    finally:
        await release_lock(database, SETTLE_LOCK_KEY, owner_token)


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
    summary = await _run_settle_leg(database, repositories, settlement, alerts)
    if summary is None:
        return 0

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
            lease_lost, on_lease_lost = _lease_lost_signal(alerts, RECONCILE_LOCK_KEY, owner_token)
            summary = await run_with_lock_renewal(
                database, RECONCILE_LOCK_KEY, owner_token, RECONCILE_LOCK_TTL_SECONDS,
                run_reconciliation(repositories, broker, settlement, alerts, lease_lost=lease_lost),
                on_renewal_failed=on_lease_lost,
            )
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


async def _run_dashboard(settings: Settings, port: int) -> int:
    """`tradepulse dashboard`: the local operator dashboard -- read-only
    observability (session state, positions, opportunities, fills/
    settlement, PnL, risk exposure, reconciliation/audit alerts) plus the
    exact same session-control authority as `start`/`stop`/`reset-risk`/
    `reset-integrity` (see tradepulse/web/app.py -- every control route
    calls straight into tradepulse.session_commands, never a second
    implementation). ALWAYS binds 127.0.0.1 -- there is no --host flag and
    no escape hatch: with no authentication/authorization layer yet,
    anything network-reachable here would be unauthenticated
    start/stop/reset-risk/reset-integrity access. Remote access is a later
    phase that must ship WITH authentication, not an opt-in flag that
    bypasses having one."""
    try:
        import uvicorn
    except ImportError as exc:
        raise SettingsError(
            "`tradepulse dashboard` requires the optional web dependencies -- install with `pip install -e '.[web]'`"
        ) from exc
    from tradepulse.web import build_app_state, create_app

    _require_credentials(settings, require_ai=False)
    state = await build_app_state(settings)
    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(state, frontend_dist=frontend_dist if frontend_dist.is_dir() else None)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=settings.log_level.lower())
    server = uvicorn.Server(config)
    logger.info("dashboard_starting", extra={"event": "dashboard_starting", "host": "127.0.0.1", "port": port})
    try:
        await server.serve()
    finally:
        await state.broker.aclose()
    return 0


async def _check_market_state(broker: AlpacaClient) -> Literal["open", "closed", "indeterminate"]:
    """The same live broker.get_clock() call the execution gateway's own
    submission-boundary gate already relies on, but returning a TRI-STATE
    result instead of a bool -- a broker/network failure is `"indeterminate"`,
    never silently folded into `"closed"`. This is what lets _scan_action
    (below) use a short bounded retry on a clock-check failure instead of
    either hammering (a 1s retry) or wrongly sitting quiet for a full lane
    interval (which is only correct once the market is CONFIRMED closed)."""
    try:
        clock = await broker.get_clock()
    except (AlpacaError, httpx.HTTPError) as exc:
        logger.warning("run_market_clock_check_failed", extra={"event": "run_market_clock_check_failed", "error": str(exc)})
        return "indeterminate"
    return "open" if clock.is_open else "closed"


async def _scan_action(
    asset_class: AssetClass, interval: int, database: AsyncSQLiteDatabase, repositories: PersistenceRepositories,
    ai_provider: AIProvider, market_data: AlpacaMarketDataProvider, broker: AlpacaClient, gateway: ExecutionGateway,
    settings: Settings, alerts: TelegramAlerter, capabilities: MarketDataCapabilities,
) -> float:
    """One tick of a supervised scan lane under `tradepulse run` -- decides
    whether to run a cycle right now and how long to wait before the next
    check, then delegates the actual work to _run_scan_leg unchanged. Crypto
    is a continuous market -- no clock gating, matching scan's own standalone
    behavior."""
    if asset_class != AssetClass.CRYPTO:
        market_state = await _check_market_state(broker)
        if market_state == "indeterminate":
            return MARKET_CLOCK_RETRY_SECONDS
        if market_state == "closed":
            logger.info("run_scan_skipped_market_closed", extra={"event": "run_scan_skipped_market_closed", "asset_class": asset_class.value})
            return interval
    await _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, settings, alerts, asset_class, capabilities)
    return interval


async def _monitor_action(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, broker: AlpacaClient,
    gateway: ExecutionGateway, alerts: TelegramAlerter, settings: Settings,
) -> float:
    await _run_monitor_leg(database, repositories, broker, gateway, alerts, settings)
    return MONITOR_INTERVAL_SECONDS


async def _settle_action(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, settlement: SettlementProcessor,
    alerts: TelegramAlerter,
) -> float:
    await _run_settle_leg(database, repositories, settlement, alerts)
    return SETTLE_INTERVAL_SECONDS


async def _periodic_loop(
    action: Callable[[], Awaitable[float]], shutdown: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Calls `action` repeatedly until shutdown -- `action` ITSELF decides how
    long to wait before the next call (its return value, in seconds). That's
    what lets _scan_action use a short bounded retry after an INDETERMINATE
    market-clock check vs. the full lane interval otherwise -- one generic
    primitive, not two. Never cancels an in-flight `action()` -- shutdown is
    only ever checked BETWEEN calls, in ~1s ticks, so it stays responsive
    without needing cancellation."""
    while not shutdown.is_set():
        wait_seconds = await action()
        waited = 0.0
        while waited < wait_seconds and not shutdown.is_set():
            await sleep(1)
            waited += 1


async def _supervised_lane(
    name: str, loop_coro: Awaitable[None], repositories: PersistenceRepositories, alerts: TelegramAlerter,
) -> None:
    """A lane dying must never (a) silently vanish with no trace, (b) take
    down its siblings, or (c) leave the session looking healthy/ACTIVE while
    nothing is actually scanning. Deliberately NOT RISK_STOPPED/
    FINANCIAL_INTEGRITY_BLOCKED -- real domain states with their own specific
    meanings this must not borrow. The persisted, typed AuditEvent below is
    already queryable and dashboard-visible today via the existing
    AlertsPanel (polls audit_events, highlights severity="critical") -- zero
    new dashboard code required."""
    try:
        await loop_coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a lane dying unhandled IS the critical condition to report; must never crash the whole supervisor or vanish silently
        message = f"TRADING_SUPERVISOR_LANE_FAILED: {name} stopped scheduling after an unhandled error: {exc}"
        logger.error("trading_supervisor_lane_failed", extra={"event": "trading_supervisor_lane_failed", "lane": name, "error": str(exc)})
        await alerts.send("critical", message, {"lane": name})
        event = AuditEvent(
            event_id=str(uuid4()), event_type="trading_supervisor_lane_failed", severity="critical",
            message=message, occurred_at=datetime.now(UTC), entity_type="trading_supervisor", entity_id=name,
            details={"lane": name, "error": str(exc)},
        )
        await repositories.audit_events.create_once(event.event_id, event)


async def _run_trading_supervisor(
    database: AsyncSQLiteDatabase, repositories: PersistenceRepositories, ai_provider: AIProvider,
    market_data: AlpacaMarketDataProvider, broker: AlpacaClient, gateway: ExecutionGateway,
    settlement: SettlementProcessor, settings: Settings, alerts: TelegramAlerter, shutdown: asyncio.Event,
    capabilities: MarketDataCapabilities, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Launches ONE independent asyncio task per lane (equity, crypto,
    option, monitor, settle) -- genuine concurrency, never a shared serial
    loop, so a slow equity AI call can never delay a simultaneously-due
    crypto/option cycle or the position monitor. Each lane is wrapped in
    _supervised_lane so one lane's crash never affects its siblings.
    asyncio.gather only returns once every lane's task has ended (i.e. at
    shutdown, since _supervised_lane catches each lane's own exceptions
    rather than letting them propagate) -- this still correctly waits for
    any lane's in-flight cycle to finish before returning, upholding "never
    cancel in-flight work" at the whole-supervisor level too."""
    lanes: dict[str, Awaitable[None]] = {
        "equity": _periodic_loop(
            lambda: _scan_action(
                AssetClass.EQUITY, EQUITY_SCAN_INTERVAL_SECONDS, database, repositories, ai_provider, market_data,
                broker, gateway, settings, alerts, capabilities,
            ),
            shutdown, sleep,
        ),
        "crypto": _periodic_loop(
            lambda: _scan_action(
                AssetClass.CRYPTO, CRYPTO_SCAN_INTERVAL_SECONDS, database, repositories, ai_provider, market_data,
                broker, gateway, settings, alerts, capabilities,
            ),
            shutdown, sleep,
        ),
        "option": _periodic_loop(
            lambda: _scan_action(
                AssetClass.OPTION, OPTION_SCAN_INTERVAL_SECONDS, database, repositories, ai_provider, market_data,
                broker, gateway, settings, alerts, capabilities,
            ),
            shutdown, sleep,
        ),
        "monitor": _periodic_loop(lambda: _monitor_action(database, repositories, broker, gateway, alerts, settings), shutdown, sleep),
        "settle": _periodic_loop(lambda: _settle_action(database, repositories, settlement, alerts), shutdown, sleep),
    }
    await asyncio.gather(*(_supervised_lane(name, coro, repositories, alerts) for name, coro in lanes.items()))


def _build_dashboard_server(state: Any, port: int, log_level: str) -> Any:
    """Returns an unstarted uvicorn.Server -- shared construction helper for
    `_run_application`'s task-based dashboard, mirroring _run_dashboard's own
    app/config assembly but not literally reusing that function, since the
    standalone command's own SIGINT/SIGTERM handling (uvicorn's default) must
    stay exactly as it is today (see _run_dashboard's docstring)."""
    import uvicorn

    from tradepulse.web import create_app

    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(state, frontend_dist=frontend_dist if frontend_dist.is_dir() else None)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level.lower())
    return uvicorn.Server(config)


async def _run_dashboard_server(server: Any, shutdown: asyncio.Event) -> None:
    """Runs `server` until `shutdown` fires, then asks uvicorn to stop and
    awaits it -- the task-based counterpart to _run_dashboard's blocking
    `await server.serve()`, so `tradepulse run` can manage the dashboard
    alongside the trading supervisor instead of blocking on it."""
    logger.info("dashboard_starting", extra={"event": "dashboard_starting", "host": "127.0.0.1", "port": server.config.port})
    serve_task = asyncio.create_task(server.serve())
    await shutdown.wait()
    server.should_exit = True
    await serve_task


async def _open_browser_when_ready(
    server: Any, port: int, shutdown: asyncio.Event,
    poll_interval: float = BROWSER_OPEN_POLL_SECONDS, max_wait: float = BROWSER_OPEN_MAX_WAIT_SECONDS,
) -> None:
    """Non-fatal by construction: a headless box, no $DISPLAY, or an
    unsupported platform must never affect the dashboard or trading
    supervisor -- any failure here is caught and only logged."""
    waited = 0.0
    while not server.started and waited < max_wait and not shutdown.is_set():
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    if shutdown.is_set():
        return
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception as exc:  # noqa: BLE001 -- browser-launch failure must never affect the dashboard/trading supervisor
        logger.warning("run_browser_open_failed", extra={"event": "run_browser_open_failed", "error": str(exc)})


async def _run_application(settings: Settings, port: int, open_browser: bool) -> int:
    """`tradepulse run`: the normal one-command interactive startup -- opens
    the local dashboard, activates the trading session, and keeps the
    equity/crypto/option scan lanes, position monitor, and settlement
    cycling on their own independent schedules (see _run_trading_supervisor)
    until Ctrl+C. See the module docstring for how this relates to every
    other, one-shot command."""
    _require_credentials(settings, require_ai=True)  # run always scans -- needs both broker AND AI creds
    try:
        import uvicorn  # noqa: F401 -- checked early so a missing web extra fails fast, before any broker/DB setup
    except ImportError as exc:
        raise SettingsError(
            "`tradepulse run` requires the optional web dependencies -- install with `pip install -e '.[web]'`"
        ) from exc
    from tradepulse.web import AppState

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = _build_broker(settings)
    ai_provider = _build_ai_provider(settings)
    alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)

    async def work() -> int:
        capabilities = await _resolve_and_apply_market_data_feeds(broker, settings)
        market_data = AlpacaMarketDataProvider(broker)
        gateway = _build_gateway(settings, repositories, broker, market_data, alerts)
        settlement = SettlementProcessor(repositories, alerts)
        dashboard_state = AppState(settings=settings, repositories=repositories, broker=broker, market_data=market_data)
        server = _build_dashboard_server(dashboard_state, port, settings.log_level)

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown.set)

        dashboard_task = asyncio.create_task(_run_dashboard_server(server, shutdown))
        if open_browser:
            asyncio.create_task(_open_browser_when_ready(server, port, shutdown))

        # MARKET_CLOSED is a routine, expected state -- not a safety fault
        # like RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED/SYSTEM_DEGRADED.
        # It only ever arises from sync_market_session flipping a
        # previously-ACTIVE session overnight (risk/session.py), which
        # always preserves trading_active=True -- the session is already
        # properly started, just correctly reflecting that equities are
        # shut. Calling _run_start here would hard-refuse (matching its
        # own, correct, conservative behavior for the STANDALONE `start`
        # command, which has no way to re-verify anything's changed) and
        # previously blocked the ENTIRE supervisor -- including crypto
        # (a continuous market that already tolerates MARKET_CLOSED at
        # the execution boundary, see risk/session.py::execution_session_decision's
        # CONTINUOUS_ASSET_SESSION branch) and monitor/settlement, which
        # have nothing to do with equity market hours at all. Skip the
        # redundant/wrong-shaped activation call in exactly this one case
        # so the supervisor starts and each lane's own market-clock check
        # (_scan_action, crypto unaffected) does the correct fine-grained
        # gating instead.
        current_session = await load_session(repositories)
        if current_session.state == SessionState.MARKET_CLOSED:
            logger.info("run_session_already_active_market_closed", extra={"event": "run_session_already_active_market_closed"})
            start_result = 0
        else:
            start_result = await _run_start(settings)
        trading_task: asyncio.Task[None] | None = None
        if start_result != 0:
            logger.error("run_session_activation_failed", extra={"event": "run_session_activation_failed"})
            # Dashboard still comes up -- an operator needs to SEE why
            # activation failed (RISK_STOPPED? FINANCIAL_INTEGRITY_BLOCKED?
            # broker unreachable?) and use its controls to fix it. But no
            # scan/monitor/settlement task may start against a session the
            # authoritative activation command just refused -- downstream
            # execution gates are not a substitute for honoring that
            # refusal. v1 does not auto-detect a later fix and auto-start
            # the supervisor -- restart `tradepulse run` after correcting
            # the condition.
        else:
            trading_task = asyncio.create_task(
                _run_trading_supervisor(
                    database, repositories, ai_provider, market_data, broker, gateway, settlement, settings, alerts,
                    shutdown, capabilities,
                )
            )

        await shutdown.wait()
        # ---- graceful shutdown: stop scheduling, let in-flight work finish, stop the server, done ----
        if trading_task is not None:
            await trading_task
        await dashboard_task
        return 0

    owner_token = str(uuid4())
    if not await acquire_lock(database, RUN_LOCK_KEY, owner_token, "run", RUN_LOCK_TTL_SECONDS):
        logger.error("run_already_active", extra={"event": "run_already_active"})
        await broker.aclose()
        await ai_provider.aclose()
        return 1
    try:
        return await run_with_lock_renewal(database, RUN_LOCK_KEY, owner_token, RUN_LOCK_TTL_SECONDS, work())
    finally:
        await release_lock(database, RUN_LOCK_KEY, owner_token)
        await broker.aclose()
        await ai_provider.aclose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradepulse", description="TradePulse AI trading runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser(
        "scan", help="run one AI-driven scan cycle for one or more asset-class lanes (concurrently), plus the position monitor, then exit"
    )
    scan_parser.add_argument(
        "--asset-class", required=True, nargs="+", choices=["equity", "crypto", "option"],
        help="which lane(s) to scan this invocation -- one value runs a single lane (e.g. a lane-specific cron cadence); "
        "multiple values fan out concurrently from this one process",
    )
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
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="run the local operator dashboard (read-only observability + start/stop/reset controls); always binds 127.0.0.1, no remote option"
    )
    dashboard_parser.add_argument("--port", type=int, default=8000, help="port to bind on 127.0.0.1 (default: 8000)")
    run_parser = subparsers.add_parser(
        "run",
        help="one-command interactive startup: dashboard + session activation + parallel equity/crypto/option scan "
        "lanes, monitor, and settlement, cycling independently until Ctrl+C; always binds 127.0.0.1, no remote option",
    )
    run_parser.add_argument("--port", type=int, default=8000, help="port to bind the dashboard on 127.0.0.1 (default: 8000)")
    run_parser.add_argument("--no-browser", action="store_true", help="don't automatically open the dashboard in a browser")
    return parser


_COMMANDS: dict[str, Any] = {
    "monitor": _run_monitor, "settle": _run_settle, "reconcile": _run_reconcile,
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
        if args.command == "scan":
            return asyncio.run(_run_scan(settings, [AssetClass(v) for v in args.asset_class]))
        if args.command == "dashboard":
            return asyncio.run(_run_dashboard(settings, args.port))
        if args.command == "run":
            return asyncio.run(_run_application(settings, args.port, not args.no_browser))
        return asyncio.run(_COMMANDS[args.command](settings))
    except (SettingsError, MarketDataCapabilityError) as exc:
        print(f"tradepulse: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
