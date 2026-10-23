"""Account lease (Phase A0 Task 7, Rev.136): a host-local exclusive claim on
one broker account, held by every order-capable command and re-verified by
the gateway before every submission."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_cli import _settings, _stub_build_dashboard_server

from tradepulse import cli
from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    SessionState,
    Side,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.risk import save_session
from tradepulse.runtime_lease import (
    AccountLease,
    AccountLeaseHeld,
    AccountLeaseUnavailable,
    account_lease,
    account_lease_in_use,
    lease_directory,
    lease_path,
)
from tradepulse.settlement import SettlementProcessor

ROOT = Path(__file__).resolve().parent.parent
REF = AccountRef("alpaca", "PA0001")
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)  # Monday, session open


# --- the lock -------------------------------------------------------------------


def test_second_holder_is_refused_and_named(tmp_path: Path) -> None:
    named = rf"alpaca:PA0001 is leased by tradepulse run \(pid {os.getpid()},"
    with (account_lease(REF, holder="tradepulse run", directory=tmp_path),
          pytest.raises(AccountLeaseHeld, match=named),
          account_lease(REF, holder="tradepulse scan", directory=tmp_path)):
        pass
    with account_lease(REF, holder="later", directory=tmp_path) as lease:  # released on exit
        lease.verify()


def test_lease_frees_when_the_holder_process_dies(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            from tradepulse.broker.port import AccountRef
            from tradepulse.runtime_lease import account_lease
            with account_lease(AccountRef("alpaca", "PA0001"), holder="doomed", directory=Path({str(tmp_path)!r})):
                print("held", flush=True)
                time.sleep(600)
        """)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(AccountLeaseHeld, match="doomed"), account_lease(REF, holder="me", directory=tmp_path):
            pass
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        with account_lease(REF, holder="me", directory=tmp_path) as lease:
            lease.verify()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_probe_does_not_hold_the_lease(tmp_path: Path) -> None:
    assert account_lease_in_use(REF, directory=tmp_path) is None
    assert not lease_path(REF, tmp_path).exists()  # a probe creates nothing
    with account_lease(REF, holder="first", directory=tmp_path):
        assert account_lease_in_use(REF, directory=tmp_path).startswith("first (pid ")  # type: ignore[union-attr]
    assert account_lease_in_use(REF, directory=tmp_path) is None
    assert account_lease_in_use(REF, directory=tmp_path) is None  # probing repeatedly takes nothing
    with account_lease(REF, holder="second", directory=tmp_path):
        pass


def test_replaced_lock_file_fails_verify(tmp_path: Path) -> None:
    with account_lease(REF, holder="me", directory=tmp_path) as lease:
        lease.verify()
        lease.path.unlink()
        with pytest.raises(AccountLeaseUnavailable, match="deleted or replaced"):
            lease.verify()
        lease.path.write_text("")
        with pytest.raises(AccountLeaseUnavailable, match="deleted or replaced"):
            lease.verify()
        # Why it matters: the replacement inode is lockable by anyone else.
        with account_lease(REF, holder="intruder", directory=tmp_path):
            pass
    with pytest.raises(AccountLeaseUnavailable, match="released"):
        lease.verify()


def test_lease_files_are_never_unlinked_and_are_private(tmp_path: Path) -> None:
    with account_lease(REF, holder="me", directory=tmp_path) as lease:
        path = lease.path
    assert path == tmp_path / "alpaca-PA0001.lock" and path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_symlinked_lock_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("")
    lease_path(REF, tmp_path).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(AccountLeaseUnavailable), account_lease(REF, holder="me", directory=tmp_path):
        pass


def test_different_accounts_do_not_conflict(tmp_path: Path) -> None:
    with account_lease(REF, holder="a", directory=tmp_path) as first, \
         account_lease(AccountRef("alpaca", "PA0002"), holder="b", directory=tmp_path) as second, \
         account_lease(AccountRef("ibkr", "PA0001"), holder="c", directory=tmp_path) as third:
        for lease in (first, second, third):
            lease.verify()


# --- the directory ----------------------------------------------------------------


def test_lease_directory_resolution_and_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEPULSE_LEASE_DIR", str(tmp_path / "explicit"))
    assert lease_directory() == tmp_path / "explicit"
    assert (tmp_path / "explicit").stat().st_mode & 0o777 == 0o700
    monkeypatch.delenv("TRADEPULSE_LEASE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert lease_directory() == tmp_path / "state" / "tradepulse" / "leases"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert lease_directory() == tmp_path / "home" / ".local" / "state" / "tradepulse" / "leases"


@pytest.mark.parametrize("kind", ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"])
def test_network_filesystem_directory_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    mount = tmp_path / "remote share"
    mountinfo = tmp_path / "mountinfo"
    escaped = str(mount).replace(" ", "\\040")
    mountinfo.write_text(
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
        f"98 22 0:51 / {escaped} rw,relatime shared:40 - {kind} server:/export rw\n"
    )
    monkeypatch.setattr("tradepulse.runtime_lease.MOUNTINFO", mountinfo)
    monkeypatch.setenv("TRADEPULSE_LEASE_DIR", str(mount / "leases"))
    with pytest.raises(AccountLeaseUnavailable, match=kind):
        lease_directory()
    monkeypatch.setenv("TRADEPULSE_LEASE_DIR", str(tmp_path / "local"))
    assert lease_directory() == tmp_path / "local"  # the root mount is ext4


# --- the gateway --------------------------------------------------------------------


async def _gateway(tmp_path: Path, lease: AccountLease) -> tuple[PersistenceRepositories, SimulatedBroker,
                                                                  ExecutionGateway]:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = SimulatedBroker(clock=ManualClock(NOW + timedelta(seconds=1)),
                             session=SimSession.from_exchange_calendar("XNYS", date(2026, 8, 1), date(2026, 9, 30)))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("199.50"), Decimal("199.60"), at=NOW)
    alerts = TelegramAlerter(None, None)
    gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker),
                               SettlementProcessor(repositories, alerts, clock=lambda: NOW), alerts,
                               risk_limits_for_profile("balanced"), ExecutionMode.PAPER, clock=lambda: NOW, lease=lease)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    return repositories, broker, gateway


def _buy() -> ExecutionRequest:
    return ExecutionRequest(asset=AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL"), side=Side.BUY,
                            requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))


def test_gateway_cannot_be_built_without_a_lease(tmp_path: Path) -> None:
    alerts = TelegramAlerter(None, None)
    broker = SimulatedBroker(clock=ManualClock(NOW), session=SimSession.always_open())
    with pytest.raises(TypeError, match="lease"):
        ExecutionGateway(None, broker, None, None, alerts, None, ExecutionMode.PAPER)  # type: ignore[arg-type, call-arg]
    with (account_lease(AccountRef("ibkr", "U1"), holder="me", directory=tmp_path) as foreign,
          pytest.raises(ValueError, match="venue")):
        ExecutionGateway(None, broker, None, None, alerts, None, ExecutionMode.PAPER, lease=foreign)  # type: ignore[arg-type]
    # The omission is also a static error.
    program = textwrap.dedent("""
        from tradepulse.execution import ExecutionGateway
        from tradepulse.session_commands import build_gateway
        ExecutionGateway(None, None, None, None, None, None, None)  # type: ignore[arg-type]
        build_gateway(None, None, None, None, None)  # type: ignore[arg-type]
    """)
    snippet = tmp_path / "no_lease.py"
    snippet.write_text(program)
    result = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary", str(snippet)],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert 'Missing named argument "lease" for "ExecutionGateway"' in result.stdout, result.stdout + result.stderr
    assert 'Missing positional argument "lease" in call to "build_gateway"' in result.stdout, result.stdout


async def test_lost_lease_rejects_before_any_broker_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="me", directory=tmp_path) as lease:
        repositories, broker, gateway = await _gateway(tmp_path, lease)
        sent: list[tuple[str, str]] = []

        async def record(self: TelegramAlerter, severity: str, message: str, details: object = None) -> None:
            sent.append((severity, message))

        async def no_order(*args: object, **kwargs: object) -> None:
            raise AssertionError("place_order must never be reached")

        monkeypatch.setattr(TelegramAlerter, "send", record)
        monkeypatch.setattr(broker, "place_order", no_order)
        lease.path.unlink()  # break verify(): the lock file is gone
        lease.path.write_text("")
        result = await gateway.execute_intent(_buy())
    assert (result.status, result.reasons) == ("rejected", ["ACCOUNT_LEASE_NOT_HELD"])
    [row] = await repositories.trade_intents.list_all()
    intent = hydrate("trade_intents", row["payload"])
    assert (intent.status, intent.rejection_reason) == (TradeIntentStatus.REJECTED, "ACCOUNT_LEASE_NOT_HELD")
    assert broker._orders == {}
    assert any(severity == "critical" and "not held" in message for severity, message in sent)


async def test_held_lease_lets_the_order_through(tmp_path: Path) -> None:
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="me", directory=tmp_path) as lease:
        _, broker, gateway = await _gateway(tmp_path, lease)
        result = await gateway.execute_intent(_buy())
    assert (result.status, result.filled_quantity) == ("filled", Decimal("5"))
    assert len(broker._orders) == 1


# --- the commands -------------------------------------------------------------------


def _simulated_commands(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every order-capable command runs against a SimulatedBroker (account
    alpaca:SIM0001); anything order-capable it reaches is recorded."""
    reached: list[str] = []
    monkeypatch.setattr(cli, "_build_broker",
                        lambda settings: SimulatedBroker(clock=ManualClock(NOW), session=SimSession.always_open()))

    def reach(name: str) -> object:
        def record(*args: object, **kwargs: object) -> object:
            reached.append(name)
            raise AssertionError(f"{name} reached while the lease is held elsewhere")
        return record

    for name in ("_build_gateway", "_run_trading_supervisor", "_run_scan_leg", "_run_monitor_leg"):
        monkeypatch.setattr(cli, name, reach(name))
    return reached


async def test_each_order_capable_command_refuses_when_the_lease_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    reached = _simulated_commands(monkeypatch)
    monkeypatch.setattr(cli, "_build_dashboard_server", _stub_build_dashboard_server)
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="another runtime", directory=lease_directory()):
        assert await cli._run_scan(settings, [AssetClass.EQUITY]) == 1
        assert await cli._run_monitor(settings) == 1
        assert await cli._run_application(settings, 8131, False) == 1
    assert reached == []
    held = [r for r in caplog.records if getattr(r, "event", None) == "account_lease_held"]
    assert len(held) == 3 and all("another runtime" in r.detail for r in held)  # type: ignore[attr-defined]


async def test_lease_released_only_after_order_capable_tasks_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The run is torn down (cancelled, as Ctrl+C and any propagating
    exception both end it) while the trading supervisor is mid-flight. The
    lease's release hook sees the supervisor already finished."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cli, "_build_broker",
                        lambda settings: SimulatedBroker(clock=ManualClock(NOW), session=SimSession.always_open()))
    monkeypatch.setattr(cli, "_build_dashboard_server", _stub_build_dashboard_server)

    async def stub_dashboard(server: object, shutdown: asyncio.Event) -> None:
        await shutdown.wait()

    async def stub_start(settings: object) -> int:
        return 0

    supervisor: list[asyncio.Task[None]] = []
    started = asyncio.Event()

    async def stub_supervisor(*args: object, **kwargs: object) -> None:
        supervisor.append(asyncio.current_task())  # type: ignore[arg-type]
        started.set()
        await asyncio.Event().wait()  # in flight until cancelled

    released_with_supervisor_done: list[bool] = []
    real_lease = cli.account_lease

    @contextlib.contextmanager
    def watched_lease(ref: AccountRef, *, holder: str) -> Iterator[AccountLease]:
        with real_lease(ref, holder=holder) as lease:
            try:
                yield lease
            finally:  # runs just before the real lease is released
                released_with_supervisor_done.append(supervisor[0].done())

    monkeypatch.setattr(cli, "_run_dashboard_server", stub_dashboard)
    monkeypatch.setattr(cli, "_run_start", stub_start)
    monkeypatch.setattr(cli, "_run_trading_supervisor", stub_supervisor)
    monkeypatch.setattr(cli, "account_lease", watched_lease)
    settings = _settings(f"sqlite:///{tmp_path}/test.db")

    run = asyncio.create_task(cli._run_application(settings, 8132, False))
    await asyncio.wait_for(started.wait(), timeout=10)
    assert account_lease_in_use(AccountRef("alpaca", "SIM0001")) is not None
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert released_with_supervisor_done == [True]
    assert account_lease_in_use(AccountRef("alpaca", "SIM0001")) is None
    [acquired] = [r for r in caplog.records if getattr(r, "event", None) == "account_lease_acquired"]
    assert (acquired.venue, acquired.account_number, acquired.holder) == ("alpaca", "SIM0001", "tradepulse run")  # type: ignore[attr-defined]


async def test_run_takes_the_account_lease_before_the_run_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Held account lease: `run` exits before touching the per-database run
    lock, so the run lock is still free afterwards."""
    from tradepulse.persistence import acquire_lock

    _simulated_commands(monkeypatch)
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="other", directory=lease_directory()):
        assert await cli._run_application(settings, 8133, False) == 1
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    assert await acquire_lock(database, cli.RUN_LOCK_KEY, "probe", "run", ttl_seconds=60) is True
