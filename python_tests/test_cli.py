import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from os import environ

import httpx
import pytest
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.cli import (
    MONITOR_LOCK_KEY,
    RECONCILE_LOCK_KEY,
    SCAN_LOCK_KEY,
    SETTLE_LOCK_KEY,
    _build_ai_provider,
    _build_parser,
    _lease_lost_signal,
    _load_dotenv,
    _require_credentials,
    _run_monitor,
    _run_reconcile,
    _run_scan,
    _run_scan_leg,
    _run_settle,
)
from tradepulse.config import Settings, SettingsError
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, hydrate
from tradepulse.providers import AnthropicAIProvider, OpenAIProvider


def test_scan_subcommand_parses() -> None:
    args = _build_parser().parse_args(["scan"])
    assert args.command == "scan"


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
    return Settings.from_env({
        "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key",
        "TRADEPULSE_DATABASE_URL": database_url, **extra,
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
    assert await acquire_lock(database, SCAN_LOCK_KEY, "other-owner", "scan", ttl_seconds=600) is True

    positions_route = respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[]))

    exit_code = await _run_scan(_settings(database_url))

    assert exit_code == 0
    assert positions_route.call_count == 1  # monitor leg ran despite scan's lock being held


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
    holding_row = await repositories.holdings.get("AAPL")
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
        await _reassign_owner(database, SCAN_LOCK_KEY, "owner-other")  # a legitimate takeover after expiry
        await asyncio.sleep(1.0)  # long enough for the next heartbeat tick to observe the theft
        return "stub-result"

    monkeypatch.setattr("tradepulse.cli.run_scan_cycle", _stub_scan_cycle)

    broker = None
    ai_provider = None
    market_data = None
    gateway = None
    alerts = TelegramAlerter(None, None)

    with caplog.at_level("WARNING"):
        result = await _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, _settings(database_url), alerts)

    assert result == "stub-result"  # never cancelled despite the lost lease
    assert observed_lease_lost["event"].is_set()
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert any("Lock renewal failed for '" + SCAN_LOCK_KEY + "'" in r.alert_message for r in skipped)
