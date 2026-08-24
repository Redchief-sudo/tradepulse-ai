from datetime import UTC, datetime, timedelta
from decimal import Decimal
from os import environ

import httpx
import pytest
import respx

from tradepulse.cli import (
    MONITOR_LOCK_KEY,
    RECONCILE_LOCK_KEY,
    SCAN_LOCK_KEY,
    SETTLE_LOCK_KEY,
    _build_ai_provider,
    _build_parser,
    _load_dotenv,
    _require_credentials,
    _run_monitor,
    _run_reconcile,
    _run_scan,
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
