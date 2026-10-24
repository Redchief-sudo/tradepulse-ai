"""The conformance kit's broker fixture and its order guard.

``conformant_broker`` yields one ``Venue`` per adapter:

- ``simulated_xnys`` and ``simulated_always_open``: SimulatedBroker on the
  real XNYS calendar, and on an always-open one (crypto's continuous market);
- ``alpaca_paper``: the configured Alpaca paper account, marked
  ``integration`` (deselected unless ``-m integration``). It refuses any
  configuration that is not paper with live trading disabled, and is
  read-only unless the order guard below admits a test.

A test that places orders on a real account takes the ``order_guard``
fixture, which (in order) requires TRADEPULSE_CONFORMANCE_ORDERS=1, resolves
the AccountRef, takes the account lease (holder "conformance kit"), refuses
while any tradepulse-* user unit is active or a tradepulse runtime process is
running, and skips unless the market is open.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradepulse.broker.port import BrokerPort, OrderRequest, OrderState, account_ref
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.config import Settings
from tradepulse.models import AssetClass
from tradepulse.runtime_lease import AccountLease, AccountLeaseHeld, account_lease

ROOT = Path(__file__).resolve().parents[2]
ORDERS_ENV = "TRADEPULSE_CONFORMANCE_ORDERS"
T0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York, session open


class ConformanceRefused(RuntimeError):
    """The order guard refused to let a test place orders on a real account."""


@dataclass
class Venue:
    kind: str
    broker: BrokerPort
    sim: SimulatedBroker | None = None
    clock: ManualClock | None = None
    settings: Settings | None = None

    @property
    def simulated(self) -> bool:
        return self.sim is not None


def _simulated(kind: str) -> Venue:
    session = (SimSession.always_open() if kind == "simulated_always_open"
               else SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)))
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=session)
    for symbol, bid, ask in (("SPY", "600.00", "600.05"), ("AAPL", "199.50", "199.60")):
        broker.set_quote(symbol, AssetClass.EQUITY, Decimal(bid), Decimal(ask), at=T0)
    return Venue(kind, broker, broker, clock)


def paper_settings() -> Settings:
    """The operator's configuration, as the CLI loads it (.env fills only
    keys the environment does not set). Refused unless paper with live
    trading disabled."""
    from tradepulse.cli import _load_dotenv

    _load_dotenv(ROOT / ".env")
    settings = Settings.from_env()
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise ConformanceRefused("the conformance kit runs only against paper with live trading disabled")
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise ConformanceRefused("ALPACA_API_KEY/ALPACA_API_SECRET are not configured")
    return settings


@pytest.fixture(params=[
    "simulated_xnys",
    "simulated_always_open",
    pytest.param("alpaca_paper", marks=pytest.mark.integration),
])
async def conformant_broker(request: pytest.FixtureRequest) -> AsyncIterator[Venue]:
    if request.param != "alpaca_paper":
        venue = _simulated(request.param)
    else:
        from tradepulse.session_commands import build_broker

        settings = paper_settings()
        venue = Venue("alpaca_paper", build_broker(settings), settings=settings)
    try:
        yield venue
    finally:
        await venue.broker.aclose()


# --- the order guard -----------------------------------------------------------------


def active_tradepulse_units() -> list[str]:
    """Active tradepulse-* systemd user units. A probe that cannot run counts
    as active: the guard fails closed."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "tradepulse-*", "--state=active", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"(systemctl unavailable: {exc})"]
    if result.returncode != 0:
        return [f"(systemctl failed: {result.stderr.strip()})"]
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


_RUNTIME_COMMANDS = frozenset({"run", "scan", "monitor"})


def running_tradepulse_runtimes(proc: Path = Path("/proc")) -> list[str]:
    """Order-capable TradePulse processes other than this one: `tradepulse
    run|scan|monitor` (console script or `-m tradepulse`) and the soak runner.

    Fails closed: a process whose command line cannot be read, or a /proc
    that cannot be listed, is reported as a possible runtime, so the guard
    refuses. Only a process that exited mid-scan is passed over. (A /proc
    mounted with hidepid hides other users' processes entirely; the lease
    does not cover other users either -- see runtime_lease.py.)"""
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        return [f"(cannot list {proc}: {exc})"]
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited while we looked
        except OSError as exc:
            found.append(f"{entry.name}: (command line unreadable: {exc})")
            continue
        args = [a.decode(errors="replace") for a in argv if a]
        for index, arg in enumerate(args):
            is_cli = Path(arg).name == "tradepulse" or (arg == "-m" and args[index + 1:index + 2] == ["tradepulse"])
            following = args[index + (2 if arg == "-m" else 1):]
            if (is_cli and following and following[0] in _RUNTIME_COMMANDS) or arg.endswith("run_accounting_soak.py"):
                found.append(f"{entry.name}: {' '.join(args)}")
                break
    return found


@asynccontextmanager
async def conformance_order_guard(
    broker: BrokerPort, settings: Settings, *, environ: Mapping[str, str] = os.environ,
    units: Callable[[], list[str]] = active_tradepulse_units,
    processes: Callable[[], list[str]] = running_tradepulse_runtimes,
) -> AsyncIterator[AccountLease]:
    if environ.get(ORDERS_ENV) != "1":
        pytest.skip(f"order tests need {ORDERS_ENV}=1")
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise ConformanceRefused("order tests run only against paper with live trading disabled")
    ref = await account_ref(broker)
    try:
        with account_lease(ref, holder="conformance kit") as lease:
            active = units()
            if active:
                raise ConformanceRefused(f"tradepulse user units are active: {active}")
            running = processes()
            if running:
                raise ConformanceRefused(f"tradepulse runtimes are running: {running}")
            if not (await broker.get_clock()).is_open:
                pytest.skip("the market is closed")
            yield lease
    except AccountLeaseHeld as exc:
        raise ConformanceRefused(f"the account lease is held: {exc}") from exc


class ConformanceCleanupFailed(RuntimeError):
    """Cleanup could not prove the account is back where it started. Lists
    every unresolved order and position for the operator."""


class OrderLedger:
    """Every order a conformance test places goes through ``place``, which
    records the client order id before submitting (so even an ambiguous
    submission is tracked) and the position in that symbol before the first
    order in it. ``cleanup`` runs however the test ends: it cancels any order
    still open, then requires each symbol's position to be back at its
    opening quantity. Whatever it cannot resolve -- an order it cannot look
    up or cancel, a position that did not return -- is reported by raising
    ConformanceCleanupFailed. It never submits an offsetting order: with an
    order's outcome uncertain, a "flattening" trade could double the exposure."""

    TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})

    def __init__(self, venue: Venue) -> None:
        self.venue = venue
        self.client_order_ids: list[str] = []
        self.opening: dict[str, Decimal] = {}

    async def _quantity(self, symbol: str) -> Decimal:
        return sum((p.qty for p in await self.venue.broker.get_positions() if p.symbol == symbol), Decimal(0))

    async def place(self, order: OrderRequest) -> OrderState:
        assert order.client_order_id, "conformance orders carry a client order id"
        if order.symbol not in self.opening:
            self.opening[order.symbol] = await self._quantity(order.symbol)
        self.client_order_ids.append(order.client_order_id)
        return await self.venue.broker.place_order(order)

    async def _settle_time(self) -> None:
        if self.venue.clock is not None:
            self.venue.clock.advance(timedelta(seconds=1))
        else:
            await asyncio.sleep(1)

    async def cleanup(self, *, wait_seconds: int = 10) -> None:
        broker, unresolved = self.venue.broker, []
        for client_order_id in self.client_order_ids:
            try:
                order = await broker.get_order_by_client_order_id(client_order_id)
            except Exception as exc:  # noqa: BLE001 - any failure leaves the order's state unknown
                unresolved.append(f"order {client_order_id}: lookup failed ({exc!r}); state unknown")
                continue
            if order is None or order.status in self.TERMINAL:
                continue
            try:
                await broker.cancel_order(order.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                unresolved.append(f"order {client_order_id} ({order.broker_order_id}, {order.status}): "
                                  f"cancel failed ({exc!r})")
                continue
            for _ in range(wait_seconds):
                try:
                    order = await broker.get_order(order.broker_order_id)
                except Exception as exc:  # noqa: BLE001
                    unresolved.append(f"order {client_order_id}: lookup after cancel failed ({exc!r})")
                    break
                if order.status in self.TERMINAL:
                    break
                await self._settle_time()
            else:
                unresolved.append(f"order {client_order_id} ({order.broker_order_id}) still {order.status} "
                                  f"{wait_seconds}s after cancel")
        for symbol, opening in self.opening.items():
            try:
                now = await self._quantity(symbol)
            except Exception as exc:  # noqa: BLE001
                unresolved.append(f"{symbol} position: lookup failed ({exc!r})")
                continue
            if now != opening:
                unresolved.append(f"{symbol} position is {now}, it was {opening} before the test: "
                                  f"unresolved exposure of {now - opening}")
        if unresolved:
            raise ConformanceCleanupFailed(
                "conformance orders left the account changed; resolve by hand, no offsetting order was sent: "
                + "; ".join(unresolved))


@pytest.fixture
async def order_ledger(conformant_broker: Venue, order_guard: object) -> AsyncIterator[OrderLedger]:
    """Orders placed through the ledger are cleaned up and verified however
    the test ends, while the order guard (and on paper, the account lease)
    is still held."""
    ledger = OrderLedger(conformant_broker)
    try:
        yield ledger
    finally:
        await ledger.cleanup()


@pytest.fixture
async def order_guard(conformant_broker: Venue) -> AsyncIterator[AccountLease | None]:
    """Admits a test to place orders. The simulator needs no guard."""
    if conformant_broker.simulated:
        yield None
        return
    assert conformant_broker.settings is not None
    async with conformance_order_guard(conformant_broker.broker, conformant_broker.settings) as lease:
        yield lease
