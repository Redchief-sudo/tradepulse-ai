import httpx
import pytest
import respx

from tradepulse.cli import (
    MONITOR_LOCK_KEY,
    RECONCILE_LOCK_KEY,
    SCAN_LOCK_KEY,
    _build_parser,
    _require_credentials,
    _run_monitor,
    _run_reconcile,
    _run_scan,
)
from tradepulse.config import Settings, SettingsError
from tradepulse.persistence import AsyncSQLiteDatabase, acquire_lock


def test_scan_subcommand_parses() -> None:
    args = _build_parser().parse_args(["scan"])
    assert args.command == "scan"


def test_monitor_subcommand_parses() -> None:
    args = _build_parser().parse_args(["monitor"])
    assert args.command == "monitor"


def test_reconcile_subcommand_parses() -> None:
    args = _build_parser().parse_args(["reconcile"])
    assert args.command == "reconcile"


def test_missing_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


def test_scan_requires_alpaca_and_anthropic_credentials() -> None:
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        _require_credentials(Settings.from_env({}), require_anthropic=True)
    with pytest.raises(SettingsError, match="ANTHROPIC_API_KEY"):
        _require_credentials(Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}), require_anthropic=True)
    _require_credentials(
        Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key"}),
        require_anthropic=True,
    )


def test_monitor_and_reconcile_do_not_require_anthropic_credentials() -> None:
    _require_credentials(Settings.from_env({"ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}), require_anthropic=False)
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        _require_credentials(Settings.from_env({}), require_anthropic=False)


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
