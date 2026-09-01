import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from os import environ
from typing import Any

import httpx
import pytest
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClock, AlpacaError
from tradepulse.cli import (
    CRYPTO_SCAN_INTERVAL_SECONDS,
    EQUITY_SCAN_INTERVAL_SECONDS,
    MARKET_CLOCK_RETRY_SECONDS,
    MONITOR_LOCK_KEY,
    OPTION_SCAN_INTERVAL_SECONDS,
    RECONCILE_LOCK_KEY,
    RUN_LOCK_KEY,
    SCAN_LOCK_KEY,
    SETTLE_LOCK_KEY,
    _build_ai_provider,
    _build_parser,
    _check_market_state,
    _lease_lost_signal,
    _load_dotenv,
    _periodic_loop,
    _require_credentials,
    _run_application,
    _run_monitor,
    _run_reconcile,
    _run_scan,
    _run_scan_leg,
    _run_settle,
    _run_settle_leg,
    _run_trading_supervisor,
    _scan_action,
    _supervised_lane,
    scan_lock_key,
)
from tradepulse.config import Settings, SettingsError
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    SessionState,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    TradingSession,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, hydrate, release_lock
from tradepulse.providers import AnthropicAIProvider, MarketDataCapabilities, OpenAIProvider
from tradepulse.risk import SESSION_RECORD_ID, save_session
from tradepulse.settlement import SettlementProcessor


def test_scan_subcommand_parses() -> None:
    args = _build_parser().parse_args(["scan", "--asset-class=equity"])
    assert args.command == "scan"
    assert args.asset_class == ["equity"]  # a single value still parses to a one-element list, not a bare string


def test_dashboard_subcommand_parses_with_default_port() -> None:
    args = _build_parser().parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert args.port == 8000


def test_dashboard_subcommand_accepts_only_port_no_host_flag() -> None:
    """No --host flag exists at all -- proves there's no configurable
    escape hatch to bind anywhere but 127.0.0.1 (that bind address is a
    hardcoded constant in _run_dashboard, not something argparse ever
    exposes)."""
    args = _build_parser().parse_args(["dashboard", "--port", "9000"])
    assert args.port == 9000
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["dashboard", "--host", "0.0.0.0"])


def test_run_subcommand_parses_with_default_port_and_browser_open() -> None:
    args = _build_parser().parse_args(["run"])
    assert args.command == "run"
    assert args.port == 8000
    assert args.no_browser is False


def test_run_subcommand_accepts_port_and_no_browser_no_host_flag() -> None:
    """Mirrors the existing dashboard parser test -- no --host flag exists at
    all, proving there's no configurable escape hatch to bind anywhere but
    127.0.0.1 for `run` either."""
    args = _build_parser().parse_args(["run", "--port", "9000", "--no-browser"])
    assert args.port == 9000
    assert args.no_browser is True
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["run", "--host", "0.0.0.0"])


def test_scan_subcommand_parses_multiple_asset_classes() -> None:
    args = _build_parser().parse_args(["scan", "--asset-class", "equity", "crypto", "option"])
    assert args.asset_class == ["equity", "crypto", "option"]


def test_scan_subcommand_requires_asset_class() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["scan"])


def test_monitor_subcommand_parses() -> None:
    args = _build_parser().parse_args(["monitor"])
    assert args.command == "monitor"


def test_reconcile_subcommand_parses() -> None:
    args = _build_parser().parse_args(["reconcile"])
    assert args.command == "reconcile"


def test_settle_subcommand_parses() -> None:
    args = _build_parser().parse_args(["settle"])
    assert args.command == "settle"


def test_missing_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


def test_scan_requires_alpaca_and_anthropic_credentials_by_default() -> None:
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        _require_credentials(Settings.from_env({}), require_ai=True)
    with pytest.raises(SettingsError, match="ANTHROPIC_API_KEY"):
        _require_credentials(Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}), require_ai=True)
    _require_credentials(
        Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key"}),
        require_ai=True,
    )


def test_scan_requires_openai_credentials_when_that_provider_is_selected() -> None:
    settings = Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "TRADEPULSE_AI_PROVIDER": "openai"})
    with pytest.raises(SettingsError, match="OPENAI_API_KEY"):
        _require_credentials(settings, require_ai=True)
    settings = Settings.from_env({
        "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "TRADEPULSE_AI_PROVIDER": "openai", "OPENAI_API_KEY": "key",
    })
    _require_credentials(settings, require_ai=True)  # must not also demand ANTHROPIC_API_KEY


def test_monitor_and_reconcile_do_not_require_ai_credentials() -> None:
    _require_credentials(Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}), require_ai=False)
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        _require_credentials(Settings.from_env({}), require_ai=False)


async def test_build_ai_provider_selects_anthropic_by_default() -> None:
    settings = Settings.from_env({"ANTHROPIC_API_KEY": "key"})
    provider = _build_ai_provider(settings)
    try:
        assert isinstance(provider, AnthropicAIProvider)
    finally:
        await provider.aclose()


async def test_build_ai_provider_selects_openai_when_configured() -> None:
    settings = Settings.from_env({"TRADEPULSE_AI_PROVIDER": "openai", "OPENAI_API_KEY": "key"})
    provider = _build_ai_provider(settings)
    try:
        assert isinstance(provider, OpenAIProvider)
    finally:
        await provider.aclose()


def test_load_dotenv_populates_environ(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TRADEPULSE_TEST_DOTENV_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "TRADEPULSE_TEST_DOTENV_KEY=from-file\n"
        "TRADEPULSE_TEST_DOTENV_QUOTED=\"quoted-value\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TRADEPULSE_TEST_DOTENV_QUOTED", raising=False)

    _load_dotenv(env_file)

    assert environ["TRADEPULSE_TEST_DOTENV_KEY"] == "from-file"
    assert environ["TRADEPULSE_TEST_DOTENV_QUOTED"] == "quoted-value"


def test_load_dotenv_never_overwrites_a_real_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADEPULSE_TEST_DOTENV_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("TRADEPULSE_TEST_DOTENV_KEY=from-file\n", encoding="utf-8")

    _load_dotenv(env_file)

    assert environ["TRADEPULSE_TEST_DOTENV_KEY"] == "from-shell"


def test_load_dotenv_missing_file_is_a_no_op(tmp_path) -> None:
    _load_dotenv(tmp_path / "does-not-exist.env")  # must not raise


def _settings(database_url: str, **extra: str) -> Settings:
    # ALPACA_MARKET_DATA_TIER defaults to "basic" here (a genuine no-probe
    # override, see market_data_capability.py) so existing tests that don't
    # care about capability resolution don't need extra respx mocks for the
    # SIP/OPRA probes -- override explicitly for tests that DO exercise
    # resolution (see test_market_data_capabilities_resolved_once_before_lane_fanout).
    return Settings.from_env({
        "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key",
        "TRADEPULSE_DATABASE_URL": database_url, "ALPACA_MARKET_DATA_TIER": "basic", **extra,
    })


@respx.mock
async def test_scan_leg_skipped_when_its_lock_is_held_but_monitor_leg_still_runs(tmp_path) -> None:
    """A held scan lease must short-circuit the scan leg before any AI/broker
    order call is made -- if it didn't, this test would fail via respx's
    all-mocked assertion on the first unregistered HTTP call. The monitor
    leg has its own lock and must still run (proving one leg's lost lease
    doesn't block the other)."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    assert await acquire_lock(database, scan_lock_key(AssetClass.EQUITY), "other-owner", "scan", ttl_seconds=600) is True

    positions_route = respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))

    exit_code = await _run_scan(_settings(database_url), [AssetClass.EQUITY])

    assert exit_code == 0
    assert positions_route.call_count == 1  # monitor leg ran despite scan's lock being held


@respx.mock
async def test_three_lane_concurrent_scan_locks_independently_and_survives_one_crash(tmp_path, monkeypatch) -> None:
    """The parallel multi-asset supervisor: three lanes fan out from ONE
    `_run_scan` call, each independently lock-protected (proven by each
    stub invocation observing its own distinct asset_class), and one lane
    raising must never cancel its siblings or the monitor leg --
    return_exceptions=True on the shared gather call, not asyncio.TaskGroup.
    """
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()

    observed_lanes: list[AssetClass] = []

    async def _stub_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class, **kwargs):
        observed_lanes.append(asset_class)
        if asset_class == AssetClass.CRYPTO:
            raise RuntimeError("simulated crypto-lane crash")
        return None  # None -> _log_scan_result treats it as a clean no-op, same as a lock-skip

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)
    positions_route = respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))

    exit_code = await _run_scan(_settings(database_url), [AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION])

    assert set(observed_lanes) == {AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION}  # all three ran, none skipped by another's lock
    assert positions_route.call_count == 1  # monitor leg ran despite the crypto lane's crash
    assert exit_code == 1  # the crashed lane still fails the process's own exit code

    # Every lane's lock was released, not left stuck held by the crash.
    for asset_class in (AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION):
        assert await acquire_lock(database, scan_lock_key(asset_class), "post-check", "scan", ttl_seconds=60) is True


@respx.mock
async def test_market_data_capabilities_resolved_once_before_lane_fanout(tmp_path, monkeypatch) -> None:
    """Capability resolution is a single broker-level fact, not a per-lane
    one -- it must happen exactly once, before the scan legs fan out, never
    duplicated inside each _run_scan_leg."""
    database_url = f"sqlite:///{tmp_path}/test.db"

    call_count = {"n": 0}

    async def _stub_resolve(broker, requested_tier):
        call_count["n"] += 1
        return MarketDataCapabilities("sip", "opra")

    async def _stub_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class, **kwargs):
        return None

    monkeypatch.setattr("tradepulse.cli.resolve_market_data_capabilities", _stub_resolve)
    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))

    settings = _settings(database_url, ALPACA_MARKET_DATA_TIER="auto")
    exit_code = await _run_scan(settings, [AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION])

    assert exit_code == 0
    assert call_count["n"] == 1  # not 3 -- resolved once, shared across every lane


@respx.mock
async def test_monitor_exits_cleanly_when_lock_is_held(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    assert await acquire_lock(database, MONITOR_LOCK_KEY, "other-owner", "monitor", ttl_seconds=300) is True

    exit_code = await _run_monitor(_settings(database_url))
    assert exit_code == 0


@respx.mock
async def test_reconcile_exits_cleanly_when_lock_is_held(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    assert await acquire_lock(database, RECONCILE_LOCK_KEY, "other-owner", "reconcile", ttl_seconds=600) is True

    exit_code = await _run_reconcile(_settings(database_url))
    assert exit_code == 0


async def test_settle_exits_cleanly_when_lock_is_held(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    assert await acquire_lock(database, SETTLE_LOCK_KEY, "other-owner", "settle", ttl_seconds=300) is True

    exit_code = await _run_settle(_settings(database_url))
    assert exit_code == 0


async def test_settle_drains_a_previously_failed_settlement_without_a_new_trade(tmp_path) -> None:
    """The exact gap this command closes: a RETRYABLE_FAILED event whose
    next_retry_at has passed sits unresolved forever unless something OTHER
    than a new trade drains it. No AlpacaClient/ExecutionGateway is even
    constructed here -- proving `tradepulse settle` doesn't need one."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    now = datetime.now(UTC)
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", asset, Side.BUY, ExecutionMode.PAPER, "test", now - timedelta(minutes=10),
        requested_quantity=Decimal("5"),
    )
    await repositories.trade_intents.create_once("ti-1", intent, status=intent.status.value, unique_value="idem-1")
    fill = Fill("fill-1", "ti-1", "order-1", asset, Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("150"), Decimal("0"), Decimal("0"), now - timedelta(minutes=10))
    await repositories.fills.create_once("fill-1", fill, unique_value=None)
    stuck_event = SettlementEvent(
        "se-1", "fill-1", "ti-1", asset, Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("150"), now - timedelta(minutes=10),
        status=SettlementStatus.RETRYABLE_FAILED, attempt_count=1, next_retry_at=now - timedelta(minutes=5),
    )
    await repositories.settlements.create_once("se-1", stuck_event, status=stuck_event.status.value, unique_value="fill-1")

    exit_code = await _run_settle(_settings(database_url))
    assert exit_code == 0

    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == "completed"
    holding_row = await repositories.holdings.get(asset_identity_key(asset))
    assert holding_row is not None
    assert hydrate("holdings", holding_row["payload"]).quantity == Decimal("5")


async def test_lease_lost_signal_sets_event_and_sends_critical_alert(caplog) -> None:
    """The shared (event, callback) pair every renewable command lease uses --
    each of the four commands (_run_scan_leg/_run_monitor_leg/_run_settle/
    _run_reconcile) wires this same helper as run_with_lock_renewal's
    on_renewal_failed callback."""
    alerts = TelegramAlerter(None, None)  # credential-less -- send() logs instead of hitting the network
    lease_lost, on_lease_lost = _lease_lost_signal(alerts, SCAN_LOCK_KEY, "owner-1")
    assert not lease_lost.is_set()

    with caplog.at_level("WARNING"):
        await on_lease_lost()

    assert lease_lost.is_set()
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert len(skipped) == 1
    assert "Lock renewal failed for '" + SCAN_LOCK_KEY + "'" in skipped[0].alert_message


async def _reassign_owner(database: AsyncSQLiteDatabase, lock_key: str, new_owner_token: str) -> None:
    """Directly mutates the lock row's owner_token -- simulates a competing
    acquire_lock() legitimately taking over after a real expiry, without
    needing to orchestrate real wall-clock timing races in a test (matches
    the convention already established in test_persistence_lock.py)."""

    def op(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE locks SET owner_token=? WHERE lock_key=?", (new_owner_token, lock_key))

    await database.run(op, write=True)


@respx.mock
async def test_scan_leg_alerts_and_stops_new_work_when_its_lease_is_reclaimed(tmp_path, monkeypatch, caplog) -> None:
    """End-to-end proof that _run_scan_leg's run_with_lock_renewal wiring is
    real: a scan cycle that legitimately loses its lease mid-run (a) sends a
    critical alert, (b) sets the lease_lost event threaded into
    run_scan_cycle, and (c) still lets that in-flight run_scan_cycle call
    complete and return its result -- no forced cancellation."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    monkeypatch.setattr("tradepulse.cli.SCAN_LOCK_TTL_SECONDS", 1)  # interval = max(1/3, 1) = 1s

    observed_lease_lost: dict[str, asyncio.Event] = {}

    async def _stub_scan_cycle(*args, **kwargs):
        lease_lost = kwargs["lease_lost"]
        observed_lease_lost["event"] = lease_lost
        await asyncio.sleep(0.3)
        await _reassign_owner(database, scan_lock_key(AssetClass.EQUITY), "owner-other")  # a legitimate takeover after expiry
        await asyncio.sleep(1.0)  # long enough for the next heartbeat tick to observe the theft
        return "stub-result"

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)

    broker = None
    ai_provider = None
    market_data = None
    gateway = None
    alerts = TelegramAlerter(None, None)

    with caplog.at_level("WARNING"):
        result = await _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, _settings(database_url), alerts, AssetClass.EQUITY)

    assert result == "stub-result"  # never cancelled despite the lost lease
    assert observed_lease_lost["event"].is_set()
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert any("Lock renewal failed for '" + scan_lock_key(AssetClass.EQUITY) + "'" in r.alert_message for r in skipped)


# ---- `tradepulse run` -- one-command interactive supervisor ---------------


class _StubClockBroker:
    """Duck-typed broker.get_clock() stub for _check_market_state/_scan_action
    tests -- tri-state (open / closed / a raised error), no real HTTP."""

    def __init__(self, *, is_open: bool | None = None, error: Exception | None = None) -> None:
        self._is_open = is_open
        self._error = error
        self.call_count = 0

    async def get_clock(self) -> AlpacaClock:
        self.call_count += 1
        if self._error is not None:
            raise self._error
        return AlpacaClock(is_open=bool(self._is_open), next_open=None, next_close=None, timestamp=None)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.01) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(interval)


async def test_check_market_state_reports_open_and_closed() -> None:
    assert await _check_market_state(_StubClockBroker(is_open=True)) == "open"
    assert await _check_market_state(_StubClockBroker(is_open=False)) == "closed"


async def test_check_market_state_is_indeterminate_on_alpaca_error_not_closed() -> None:
    broker = _StubClockBroker(error=AlpacaError("boom", 500, None, None, "getClock"))
    assert await _check_market_state(broker) == "indeterminate"


async def test_check_market_state_is_indeterminate_on_transport_error_not_closed() -> None:
    broker = _StubClockBroker(error=httpx.ConnectError("boom"))
    assert await _check_market_state(broker) == "indeterminate"


async def test_scan_action_crypto_never_checks_market_state(tmp_path, monkeypatch) -> None:
    """Crypto is a continuous market -- no clock gating, matching scan's own
    standalone behavior."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)

    calls: list[AssetClass] = []

    async def _stub_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class, **kwargs):
        calls.append(asset_class)

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)
    broker = _StubClockBroker(is_open=False)  # would report "closed" if it were ever checked

    wait_seconds = await _scan_action(
        AssetClass.CRYPTO, CRYPTO_SCAN_INTERVAL_SECONDS, database, repositories, None, None, broker, None,
        settings, alerts, MarketDataCapabilities("sip", "opra"),
    )

    assert wait_seconds == CRYPTO_SCAN_INTERVAL_SECONDS
    assert calls == [AssetClass.CRYPTO]
    assert broker.call_count == 0


async def test_scan_action_confirmed_closed_skips_cycle_and_waits_full_interval(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)

    called = False

    async def _stub_scan_cycle(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)
    broker = _StubClockBroker(is_open=False)

    wait_seconds = await _scan_action(
        AssetClass.EQUITY, EQUITY_SCAN_INTERVAL_SECONDS, database, repositories, None, None, broker, None,
        settings, alerts, MarketDataCapabilities("sip", "opra"),
    )

    assert wait_seconds == EQUITY_SCAN_INTERVAL_SECONDS
    assert called is False  # no cycle run, no AI call spent while the market is confirmed closed


async def test_scan_action_indeterminate_market_state_uses_bounded_retry_not_full_interval(tmp_path, monkeypatch) -> None:
    """The corrected behavior: a broker/clock hiccup must never be silently
    folded into the full 15/20-minute lane interval -- that's only correct
    once the market is CONFIRMED closed (see the test above)."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)

    called = False

    async def _stub_scan_cycle(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)
    broker = _StubClockBroker(error=httpx.ConnectError("boom"))

    wait_seconds = await _scan_action(
        AssetClass.OPTION, OPTION_SCAN_INTERVAL_SECONDS, database, repositories, None, None, broker, None,
        settings, alerts, MarketDataCapabilities("sip", "opra"),
    )

    assert wait_seconds == MARKET_CLOCK_RETRY_SECONDS
    assert wait_seconds != OPTION_SCAN_INTERVAL_SECONDS
    assert called is False


async def test_periodic_loop_calls_action_repeatedly_and_respects_its_returned_wait() -> None:
    shutdown = asyncio.Event()
    call_count = 0
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def action() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            shutdown.set()
        return 2.0

    await _periodic_loop(action, shutdown, sleep=fake_sleep)

    assert call_count == 3
    # Two full 2-tick gaps (after calls 1 and 2); the wait after call 3 is
    # skipped entirely since shutdown is already set by then.
    assert sleep_calls == [1, 1, 1, 1]


async def test_periodic_loop_awaits_inflight_action_to_completion_after_shutdown() -> None:
    """Never cancels in-flight work -- shutdown is only checked BETWEEN
    calls."""
    shutdown = asyncio.Event()
    entered = asyncio.Event()
    completed = False

    async def action() -> float:
        entered.set()
        await asyncio.sleep(0.05)
        nonlocal completed
        completed = True
        return 999.0

    task = asyncio.create_task(_periodic_loop(action, shutdown, sleep=asyncio.sleep))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    shutdown.set()  # fires WHILE action() is still in flight
    await asyncio.wait_for(task, timeout=1.0)

    assert completed is True


async def test_supervised_lane_reraises_cancellation() -> None:
    async def _cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _supervised_lane("monitor", _cancel(), None, TelegramAlerter(None, None))


async def test_supervised_lane_persists_critical_audit_event_and_sends_alert_on_unhandled_exception(tmp_path, caplog) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)

    async def _boom() -> None:
        raise RuntimeError("simulated lane crash")

    with caplog.at_level("WARNING"):
        await _supervised_lane("equity", _boom(), repositories, alerts)  # must not raise

    rows = await repositories.audit_events.list_all()
    events = [hydrate("audit_events", row["payload"]) for row in rows]
    failures = [e for e in events if e.event_type == "trading_supervisor_lane_failed"]
    assert len(failures) == 1
    assert failures[0].severity == "critical"
    assert failures[0].entity_type == "trading_supervisor"
    assert failures[0].entity_id == "equity"
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert len(skipped) == 1  # alerts.send was actually invoked


async def test_run_settle_leg_skipped_when_lock_held(tmp_path) -> None:
    """Direct proof _run_settle_leg is the exact function tradepulse settle
    now delegates to (see _run_settle) -- a held lease is a clean, logged
    no-op, not an error."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts)
    assert await acquire_lock(database, SETTLE_LOCK_KEY, "other-owner", "settle", ttl_seconds=300) is True

    result = await _run_settle_leg(database, repositories, settlement, alerts)

    assert result is None


def _supervisor_stub_scan_cycle(
    started: "list[AssetClass] | set[AssetClass]",
    *,
    block: "dict[AssetClass, tuple[asyncio.Event, asyncio.Event]] | None" = None,
):
    async def _stub(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class, **kwargs):
        if isinstance(started, set):
            started.add(asset_class)
        else:
            started.append(asset_class)
        if block and asset_class in block:
            blocked_signal, may_proceed = block[asset_class]
            blocked_signal.set()
            await may_proceed.wait()

    return _stub


async def test_run_trading_supervisor_genuine_concurrency_not_serial(tmp_path, monkeypatch) -> None:
    """The corrected design's central proof: a slow equity lane must never
    block crypto/option from starting -- each lane is its own independent
    asyncio task, not a shared serial loop that would leave crypto/option
    waiting behind equity."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)
    settlement = SettlementProcessor(repositories, alerts)

    equity_blocked = asyncio.Event()
    equity_may_proceed = asyncio.Event()
    started: set[AssetClass] = set()

    monkeypatch.setattr(
        "tradepulse.cli.run_scan_cycle",
        _supervisor_stub_scan_cycle(started, block={AssetClass.EQUITY: (equity_blocked, equity_may_proceed)}),
    )

    async def _stub_monitor(*args, **kwargs):
        return None

    monkeypatch.setattr("tradepulse.cli.run_position_monitor", _stub_monitor)
    monkeypatch.setattr(settlement, "process_pending", _stub_monitor)

    shutdown = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    broker = _StubClockBroker(is_open=True)
    capabilities = MarketDataCapabilities("sip", "opra")

    task = asyncio.create_task(
        _run_trading_supervisor(
            database, repositories, None, None, broker, None, settlement, settings, alerts, shutdown, capabilities,
            sleep=fake_sleep,
        )
    )
    try:
        await asyncio.wait_for(equity_blocked.wait(), timeout=2.0)
        await _wait_until(lambda: AssetClass.CRYPTO in started and AssetClass.OPTION in started, timeout=2.0)

        # Equity is STILL blocked (never proceeded) while crypto and option
        # have already started -- proves siblings don't wait behind a slow
        # lane, which a shared serial loop would fail.
        assert not equity_may_proceed.is_set()
        assert AssetClass.CRYPTO in started
        assert AssetClass.OPTION in started
    finally:
        equity_may_proceed.set()
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)


async def test_run_trading_supervisor_settlement_fires_independently(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)
    settlement = SettlementProcessor(repositories, alerts)

    settle_calls = 0

    async def _stub_settle(*args, **kwargs):
        nonlocal settle_calls
        settle_calls += 1

    monkeypatch.setattr(settlement, "process_pending", _stub_settle)
    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _supervisor_stub_scan_cycle([]))

    async def _stub_monitor(*args, **kwargs):
        return None

    monkeypatch.setattr("tradepulse.cli.run_position_monitor", _stub_monitor)

    shutdown = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    broker = _StubClockBroker(is_open=True)
    capabilities = MarketDataCapabilities("sip", "opra")

    task = asyncio.create_task(
        _run_trading_supervisor(
            database, repositories, None, None, broker, None, settlement, settings, alerts, shutdown, capabilities,
            sleep=fake_sleep,
        )
    )
    await _wait_until(lambda: settle_calls >= 1, timeout=2.0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert settle_calls >= 1


async def test_run_trading_supervisor_lane_failure_is_isolated_and_recorded(tmp_path, monkeypatch) -> None:
    """A crashed lane must never take its siblings down with it, and must
    never be misreported via RISK_STOPPED/FINANCIAL_INTEGRITY_BLOCKED."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    alerts = TelegramAlerter(None, None)
    settings = _settings(database_url)
    settlement = SettlementProcessor(repositories, alerts)

    other_activity: set[str] = set()

    async def _stub_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits, asset_class, **kwargs):
        if asset_class == AssetClass.EQUITY:
            raise RuntimeError("simulated equity lane crash")
        other_activity.add(asset_class.value)

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)

    async def _stub_monitor(*args, **kwargs):
        other_activity.add("monitor")

    monkeypatch.setattr("tradepulse.cli.run_position_monitor", _stub_monitor)

    async def _stub_settle(*args, **kwargs):
        other_activity.add("settle")

    monkeypatch.setattr(settlement, "process_pending", _stub_settle)

    shutdown = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    broker = _StubClockBroker(is_open=True)
    capabilities = MarketDataCapabilities("sip", "opra")

    task = asyncio.create_task(
        _run_trading_supervisor(
            database, repositories, None, None, broker, None, settlement, settings, alerts, shutdown, capabilities,
            sleep=fake_sleep,
        )
    )
    await _wait_until(lambda: {"crypto", "option", "monitor", "settle"} <= other_activity, timeout=2.0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert "equity" not in other_activity  # the crashed lane never got to append anything

    rows = await repositories.audit_events.list_all()
    events = [hydrate("audit_events", row["payload"]) for row in rows]
    failures = [e for e in events if e.event_type == "trading_supervisor_lane_failed"]
    assert len(failures) == 1
    assert failures[0].entity_id == "equity"
    assert failures[0].severity == "critical"


def _stub_build_dashboard_server(state: Any, port: int, log_level: str) -> Any:
    return object()


async def test_run_application_lock_lifecycle_refuses_duplicate_and_releases_on_shutdown(tmp_path, monkeypatch) -> None:
    """Per review: run_with_lock_renewal only ever RENEWS an already-held
    lease -- _run_application must explicitly acquire_lock before it and
    release_lock in finally, with the SAME owner_token throughout. This
    proves that whole lifecycle: a concurrent second invocation refuses
    immediately (never reaching the trading supervisor), and the first
    invocation's own release actually frees the lease for a later caller."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    settings = _settings(database_url)

    async def _stub_run_dashboard_server(server, shutdown) -> None:
        shutdown.set()

    async def _stub_run_start(settings) -> int:
        return 0

    supervisor_calls = 0

    async def _stub_run_trading_supervisor(*args, **kwargs) -> None:
        nonlocal supervisor_calls
        supervisor_calls += 1

    monkeypatch.setattr("tradepulse.cli._build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_dashboard_server", _stub_run_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_start", _stub_run_start)
    monkeypatch.setattr("tradepulse.cli._run_trading_supervisor", _stub_run_trading_supervisor)

    assert await acquire_lock(database, RUN_LOCK_KEY, "other-owner", "run", ttl_seconds=60) is True
    exit_code = await _run_application(settings, 8123, False)
    assert exit_code == 1
    assert supervisor_calls == 0  # refused before ever reaching the supervisor
    await release_lock(database, RUN_LOCK_KEY, "other-owner")

    exit_code = await _run_application(settings, 8123, False)
    assert exit_code == 0
    assert supervisor_calls == 1

    # The first run's own release actually cleared the row -- a later
    # caller can acquire it again, not just wait out an expiry.
    assert await acquire_lock(database, RUN_LOCK_KEY, "post-check", "run", ttl_seconds=60) is True


async def test_run_application_never_starts_supervisor_when_session_activation_refused(tmp_path, monkeypatch) -> None:
    """The second correction: a refused `run_start()` (RISK_STOPPED,
    FINANCIAL_INTEGRITY_BLOCKED, broker unreachable, ...) must never launch
    scan/monitor/settlement work, even though downstream execution gates
    would separately also refuse orders -- honoring the refusal must not
    depend on those gates catching it. The dashboard stays up regardless,
    so an operator can see why and fix it."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    settings = _settings(database_url)

    dashboard_ran = False

    async def _stub_run_dashboard_server(server, shutdown) -> None:
        nonlocal dashboard_ran
        dashboard_ran = True
        shutdown.set()

    async def _stub_run_start(settings) -> int:
        return 1

    supervisor_calls = 0

    async def _stub_run_trading_supervisor(*args, **kwargs) -> None:
        nonlocal supervisor_calls
        supervisor_calls += 1

    monkeypatch.setattr("tradepulse.cli._build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_dashboard_server", _stub_run_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_start", _stub_run_start)
    monkeypatch.setattr("tradepulse.cli._run_trading_supervisor", _stub_run_trading_supervisor)

    exit_code = await _run_application(settings, 8124, False)

    assert exit_code == 0  # the process itself doesn't fail -- the dashboard stays up for diagnosis
    assert dashboard_ran is True
    assert supervisor_calls == 0


async def test_run_application_starts_supervisor_when_session_already_market_closed(tmp_path, monkeypatch) -> None:
    """Regression test for a live defect: a session already sitting in
    MARKET_CLOSED (the routine overnight state sync_market_session leaves
    an ACTIVE session in -- trading_active stays True) must NOT be routed
    through _run_start, which hard-refuses from MARKET_CLOSED for
    STANDALONE `tradepulse start`'s own good reasons (it can't re-verify
    anything changed) but would wrongly block the entire supervisor --
    including crypto (a continuous market unaffected by equity hours) and
    monitor/settlement. _run_start must never even be called in this case;
    the supervisor must start directly."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    database = AsyncSQLiteDatabase(database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    await save_session(repositories, TradingSession(SESSION_RECORD_ID, SessionState.MARKET_CLOSED, True, datetime.now(UTC)))
    settings = _settings(database_url)

    async def _stub_run_dashboard_server(server, shutdown) -> None:
        shutdown.set()

    async def _stub_run_start(settings) -> int:
        raise AssertionError("run_start must not be called when the session is already MARKET_CLOSED")

    supervisor_calls = 0

    async def _stub_run_trading_supervisor(*args, **kwargs) -> None:
        nonlocal supervisor_calls
        supervisor_calls += 1

    monkeypatch.setattr("tradepulse.cli._build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_dashboard_server", _stub_run_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_start", _stub_run_start)
    monkeypatch.setattr("tradepulse.cli._run_trading_supervisor", _stub_run_trading_supervisor)

    exit_code = await _run_application(settings, 8125, False)

    assert exit_code == 0
    assert supervisor_calls == 1  # crypto/monitor/settle must still get scheduled


async def test_run_application_still_refuses_supervisor_from_a_fresh_disabled_session(tmp_path, monkeypatch) -> None:
    """Symmetry check: the MARKET_CLOSED bypass above must not accidentally
    widen to every state -- a brand-new (DISABLED) session still goes
    through the real _run_start gate exactly as before."""
    database_url = f"sqlite:///{tmp_path}/test.db"
    settings = _settings(database_url)

    async def _stub_run_dashboard_server(server, shutdown) -> None:
        shutdown.set()

    run_start_calls = 0

    async def _stub_run_start(settings) -> int:
        nonlocal run_start_calls
        run_start_calls += 1
        return 1

    supervisor_calls = 0

    async def _stub_run_trading_supervisor(*args, **kwargs) -> None:
        nonlocal supervisor_calls
        supervisor_calls += 1

    monkeypatch.setattr("tradepulse.cli._build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_dashboard_server", _stub_run_dashboard_server)
    monkeypatch.setattr("tradepulse.cli._run_start", _stub_run_start)
    monkeypatch.setattr("tradepulse.cli._run_trading_supervisor", _stub_run_trading_supervisor)

    exit_code = await _run_application(settings, 8126, False)

    assert exit_code == 0
    assert run_start_calls == 1  # the real gate still runs for a non-MARKET_CLOSED session
    assert supervisor_calls == 0
