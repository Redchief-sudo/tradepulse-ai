"""The conformance kit (design section 3.4). Every test runs against each
``conformant_broker`` variant through BrokerPort only; simulator variants
script their market first. Tests named ``test_order_*`` place orders and, on
a real account, pass the order guard (see conftest.py); every other test is
read-only."""
from __future__ import annotations

import asyncio
import decimal
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tradepulse.broker import AlpacaClient, AlpacaError, is_definitive_rejection
from tradepulse.broker.port import AccountRef, OrderRequest, OrderState, account_ref
from tradepulse.broker.simulated import (
    Fault,
    FeePosting,
    FeeRates,
    FeeScenario,
    ManualClock,
    SimSession,
    SimulatedBroker,
)
from tradepulse.config import Settings
from tradepulse.models import AssetClass, Side
from tradepulse.models.market import VENUES, asset_key_from_broker_symbol, canonical_venue
from tradepulse.runtime_lease import account_lease, lease_directory

from .conftest import (
    ORDERS_ENV,
    ROOT,
    T0,
    ConformanceCleanupFailed,
    ConformanceRefused,
    OrderLedger,
    Venue,
    conformance_order_guard,
)

FILL_WAIT_SECONDS = 30


async def _wait_terminal(venue: Venue, order_id: str) -> OrderState:
    """Simulator: advance the manual clock. Paper: poll in real time."""
    for _ in range(FILL_WAIT_SECONDS):
        order = await venue.broker.get_order(order_id)
        if order.status in ("filled", "canceled", "expired", "rejected"):
            return order
        if venue.clock is not None:
            venue.clock.advance(timedelta(seconds=1))
        else:
            await asyncio.sleep(1)
    raise AssertionError(f"order {order_id} not terminal after {FILL_WAIT_SECONDS}s")


# --- identity -------------------------------------------------------------------------


async def test_identity_round_trip(conformant_broker: Venue) -> None:
    broker = conformant_broker.broker
    assert canonical_venue(broker.venue) == broker.venue and broker.venue in VENUES
    assert broker.implementation in ("alpaca_http", "simulated")
    ref = await account_ref(broker)
    assert ref == AccountRef(broker.venue.upper(), ref.account_number) and ref.venue == broker.venue
    if conformant_broker.sim is not None:
        conformant_broker.sim.seed_position("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("190"))
    for position in await broker.get_positions():
        key = asset_key_from_broker_symbol(position.asset_class, position.symbol, venue=broker.venue)
        assert key == asset_key_from_broker_symbol(position.asset_class, position.symbol.lower(), venue=broker.venue)
        assert key.startswith(f"{position.asset_class.value}:")


# --- accept, reject, fills, recovery ---------------------------------------------------


async def test_order_rejection_is_definitive_and_places_nothing(conformant_broker: Venue,
                                                             order_ledger: OrderLedger) -> None:
    broker = conformant_broker.broker
    client_order_id = f"conformance-reject-{uuid4()}"
    with pytest.raises(AlpacaError) as rejected:
        await order_ledger.place(OrderRequest(symbol="SPY", qty=Decimal("0"), side=Side.BUY,
                                              client_order_id=client_order_id))
    assert is_definitive_rejection(rejected.value) and rejected.value.status_code in (403, 422)
    assert await broker.get_order_by_client_order_id(client_order_id) is None


async def test_order_round_trip_accepts_fills_and_recovers_by_client_order_id(
    conformant_broker: Venue, order_ledger: OrderLedger,
) -> None:
    """One 1-share SPY round trip. The simulator fills each leg in two
    scheduled partials; a paper account fills it however it fills."""
    venue, broker = conformant_broker, conformant_broker.broker
    for side in (Side.BUY, Side.SELL):
        client_order_id = f"conformance-{side.value}-{uuid4()}"
        if venue.sim is not None:
            venue.sim.schedule_fills(client_order_id, [(timedelta(seconds=1), Decimal("0.4"), None),
                                                       (timedelta(seconds=2), Decimal("0.6"), None)])
        placed = await order_ledger.place(OrderRequest(symbol="SPY", qty=Decimal("1"), side=side,
                                                       client_order_id=client_order_id))
        assert placed.broker_order_id and placed.raw["client_order_id"] == client_order_id
        recovered = await broker.get_order_by_client_order_id(client_order_id)
        assert recovered is not None and recovered.broker_order_id == placed.broker_order_id
        final = await _wait_terminal(venue, placed.broker_order_id)
        assert (final.status, final.filled_qty) == ("filled", Decimal("1"))
        fills = [a for a in await broker.get_activities(activity_type="FILL", since=T0 if venue.simulated else None)
                 if a.raw.get("order_id") == placed.broker_order_id]
        assert sum((a.qty or Decimal(0)) for a in fills) == Decimal("1")
        if venue.sim is not None:
            assert len(fills) == 2
        vwap = sum((a.qty or Decimal(0)) * (a.price or Decimal(0)) for a in fills) / Decimal("1")
        assert final.filled_avg_price is not None and abs(final.filled_avg_price - vwap) < Decimal("0.01")


async def test_unknown_client_order_id_is_not_found(conformant_broker: Venue) -> None:
    assert await conformant_broker.broker.get_order_by_client_order_id(f"conformance-absent-{uuid4()}") is None


# --- activities -------------------------------------------------------------------------


RATES = FeeRates(
    reg_per_dollar=Decimal("0.0000206"), taf_per_share=Decimal("0.000166"), taf_max_per_trade=Decimal("8.30"),
    opt_taf_per_contract=Decimal("0.00279"), cat_per_share=Decimal("0.000035"), orf_per_contract=Decimal("0.02295"),
    occ_per_contract=Decimal("0.025"), crypto_rate=Decimal("0.0025"), rounding=decimal.ROUND_UP,
)
POSTING = FeePosting(equity_batch_ny=time(20, 16), option_batch_ny=time(16, 31), occ_delay=timedelta(seconds=12),
                     cfee_asset_delay=timedelta(seconds=3), cfee_usd_delay=timedelta(hours=4, minutes=17))


async def _history(venue: Venue) -> Venue:
    """The simulator gets a trading history with fills and posted fees;
    a paper account already has one."""
    if venue.sim is None:
        return venue
    clock = ManualClock(T0)
    session = (SimSession.always_open() if venue.kind == "simulated_always_open"
               else SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)))
    sim = SimulatedBroker(clock=clock, session=session, fee_scenario=FeeScenario.observed_alpaca_2026(RATES, POSTING))
    sim.seed_position("SPY", AssetClass.EQUITY, Decimal("10"), Decimal("590"))
    sim.set_quote("SPY", AssetClass.EQUITY, Decimal("600.00"), Decimal("600.05"), at=T0)
    for coid, offsets in (("a", [1, 1, 1]), ("b", [2])):  # three fills in one millisecond, then one more
        sim.schedule_fills(coid, [(timedelta(seconds=s), Decimal("1"), None) for s in offsets])
        await sim.place_order(OrderRequest(symbol="SPY", qty=Decimal(len(offsets)), side=Side.SELL, client_order_id=coid))
    clock.set(datetime(2026, 10, 20, 1, 0, tzinfo=UTC))  # after the fee batch posts
    return Venue(venue.kind, sim, sim, clock)


async def test_fees_are_captured_once(conformant_broker: Venue) -> None:
    venue = await _history(conformant_broker)
    first = [dict(a.raw) for a in await venue.broker.get_activities(activity_type=None)]
    second = [dict(a.raw) for a in await venue.broker.get_activities(activity_type=None)]
    assert first == second
    ids = [row["id"] for row in first]
    assert len(ids) == len(set(ids))
    fees = [row for row in first if row.get("activity_type") in ("FEE", "CFEE")]
    if venue.simulated:
        assert {row.get("activity_sub_type") for row in fees} == {"REG", "TAF", "CAT"}
    batches = [(row.get("activity_sub_type"), row.get("date"), row.get("description")) for row in fees
               if row.get("activity_sub_type") in ("REG", "TAF", "CAT", "ORF")]
    assert len(batches) == len(set(batches)), "a fee batch appears more than once"


async def test_population_is_ascending_id_and_after_id_is_a_lexicographic_bound(conformant_broker: Venue) -> None:
    """The activity population is in ascending ID order; a well-formed
    page_token bounds lexicographically even when it is no activity's ID
    (between two instants and inside one millisecond); a malformed token is a
    definitive 422. Read-only on a paper account."""
    venue = await _history(conformant_broker)
    ids = [a.activity_id for a in await venue.broker.get_activities(activity_type=None)]
    assert ids == sorted(ids)
    if len(ids) < 2:
        pytest.skip("fewer than two activities to bound between")
    tokens = []
    for lower, upper in pairwise(ids):
        lower_at, upper_at = lower.split("::")[0], upper.split("::")[0]
        if lower_at != upper_at:
            tokens.append(lower_at + "::ffffffff-ffff-4fff-bfff-ffffffffffff")
        else:  # a UUID strictly between two IDs of the same millisecond
            low, high = (int(i.split("::")[1].replace("-", ""), 16) for i in (lower, upper))
            if high - low > 1:
                mid = format((low + high) // 2, "032x")
                tokens.append(f"{lower_at}::{mid[:8]}-{mid[8:12]}-{mid[12:16]}-{mid[16:20]}-{mid[20:]}")
    assert tokens
    for token in (tokens[0], tokens[len(tokens) // 2], tokens[-1]):
        assert token not in ids
        after = [a.activity_id for a in await venue.broker.get_activities(activity_type=None, after_id=token)]
        assert after == [i for i in ids if i > token]
    with pytest.raises(AlpacaError) as malformed:
        await venue.broker.get_activities(activity_type=None, after_id=ids[0] + " ")
    assert (malformed.value.status_code, malformed.value.message) == (422, "invalid page_token")
    assert is_definitive_rejection(malformed.value)


# --- account, positions, calendar -------------------------------------------------------


async def test_account_and_position_totals(conformant_broker: Venue) -> None:
    venue, broker = conformant_broker, conformant_broker.broker
    if venue.sim is not None:
        venue.sim.seed_position("AAPL", AssetClass.EQUITY, Decimal("3"), Decimal("190"))
    account = await broker.get_account()
    positions = await broker.get_positions()
    components = account.equity_components
    assert {"long_market_value", "short_market_value"} <= set(components)
    explained = account.cash + components["long_market_value"] + components["short_market_value"]
    explained -= components.get("memoposts", Decimal(0))
    assert account.equity == explained  # one response: exact
    long_value = sum((p.market_value for p in positions if p.market_value > 0), Decimal(0))
    short_value = sum((p.market_value for p in positions if p.market_value < 0), Decimal(0))
    if venue.simulated:
        assert (long_value, short_value) == (components["long_market_value"], components["short_market_value"])
    else:  # two responses, two instants: prices may have moved between them
        tolerance = Decimal("0.01") * max(abs(components["long_market_value"]), Decimal(1))
        assert abs(long_value - components["long_market_value"]) <= tolerance
        assert abs(short_value - components["short_market_value"]) <= tolerance
    assert account.last_equity > 0


async def test_calendar_matches_get_clock(conformant_broker: Venue) -> None:
    venue = conformant_broker
    clock = await venue.broker.get_clock()
    assert clock.timestamp is not None
    if venue.kind == "simulated_always_open":
        assert clock.is_open and clock.next_open is None and clock.next_close is None
        return
    today = clock.timestamp.astimezone(UTC).date()
    session = SimSession.from_exchange_calendar("XNYS", today - timedelta(days=10), today + timedelta(days=20))
    assert clock.is_open == session.is_open(clock.timestamp)
    assert clock.next_open == session.next_open(clock.timestamp)
    assert clock.next_close == session.next_close(clock.timestamp)


# --- outages (simulator only) -------------------------------------------------------------


async def test_outages_are_ambiguous_never_definitive(conformant_broker: Venue) -> None:
    sim = conformant_broker.sim
    if sim is None:
        pytest.skip("outages are injected only on the simulator")
    for fault, raised in ((Fault.DISCONNECTED, httpx.ConnectError), (Fault.RATE_LIMITED, AlpacaError)):
        sim.inject(fault)
        with pytest.raises(raised) as outage:
            await sim.get_account()
        assert not is_definitive_rejection(outage.value)
        sim.reconnect()
    client_order_id = f"conformance-lost-{uuid4()}"
    sim.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    with pytest.raises(httpx.ConnectError) as lost:
        await sim.place_order(OrderRequest(symbol="SPY", qty=Decimal("1"), side=Side.BUY, client_order_id=client_order_id))
    assert not is_definitive_rejection(lost.value)
    assert await sim.get_order_by_client_order_id(client_order_id) is not None


# --- isolation guards -----------------------------------------------------------------------


def test_default_run_deselects_paper_and_never_resolves_the_broker(tmp_path: Path) -> None:
    # Excluded from the inner run: this test, and the one that trips the
    # guard on purpose (its blocked lookup would be logged).
    sentinel = tmp_path / "resolutions.log"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "python_tests/broker_conformance", "-q", "-rA", "-p", "no:cacheprovider",
         "-o", "addopts=", "-k", "not test_default_run_deselects and not test_unmocked_broker_call"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT), "TRADEPULSE_TEST_RESOLUTION_LOG": str(sentinel)},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    deselected = re.search(r"(\d+) deselected", output)
    assert deselected and int(deselected.group(1)) >= 9, output
    assert "alpaca_paper" not in "\n".join(line for line in output.splitlines()
                                          if line.startswith(("PASSED", "SKIPPED", "FAILED", "ERROR")))
    assert not sentinel.exists() or sentinel.read_text() == ""


async def test_unmocked_broker_call_in_default_run_fails_closed() -> None:
    client = AlpacaClient("key", "secret", "paper", 5)
    try:
        with pytest.raises(RuntimeError, match="real broker host blocked in default test run"):
            await client.get_clock()
    finally:
        await client.aclose()


def _paper(**extra: str) -> Settings:
    return Settings.from_env({"ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s", **extra})


def _sim() -> SimulatedBroker:
    return SimulatedBroker(clock=ManualClock(T0), session=SimSession.from_exchange_calendar(
        "XNYS", date(2026, 10, 1), date(2026, 10, 31)))


ALLOWED = {ORDERS_ENV: "1"}


def _quiet() -> list[str]:
    return []


async def test_order_tests_refuse_while_the_account_lease_is_held() -> None:
    broker = _sim()
    with (account_lease(AccountRef("alpaca", "SIM0001"), holder="tradepulse run", directory=lease_directory()),
          pytest.raises(ConformanceRefused, match="lease is held.*tradepulse run")):
        async with conformance_order_guard(broker, _paper(), environ=ALLOWED, units=_quiet, processes=_quiet):
            raise AssertionError("admitted while the lease was held")


async def test_order_tests_refuse_live_configuration() -> None:
    live = _paper(TRADEPULSE_EXECUTION_MODE="live", TRADEPULSE_LIVE_TRADING_ENABLED="true")
    with pytest.raises(ConformanceRefused, match="paper"):
        async with conformance_order_guard(_sim(), live, environ=ALLOWED, units=_quiet, processes=_quiet):
            raise AssertionError("admitted a live configuration")


@pytest.mark.parametrize(("units", "processes", "reason"), [
    (lambda: ["tradepulse-run.service"], _quiet, "units are active"),
    (_quiet, lambda: ["4242: /usr/bin/python3 .venv/bin/tradepulse run"], "runtimes are running"),
])
async def test_order_tests_refuse_when_a_runtime_process_or_unit_is_active(units, processes, reason) -> None:
    with pytest.raises(ConformanceRefused, match=reason):
        async with conformance_order_guard(_sim(), _paper(), environ=ALLOWED, units=units, processes=processes):
            raise AssertionError("admitted while a runtime was active")


async def test_order_guard_admits_a_quiet_paper_configuration_and_holds_the_lease() -> None:
    from tradepulse.runtime_lease import account_lease_in_use

    async with conformance_order_guard(_sim(), _paper(), environ=ALLOWED, units=_quiet, processes=_quiet) as lease:
        lease.verify()
        assert account_lease_in_use(AccountRef("alpaca", "SIM0001")).startswith("conformance kit")  # type: ignore[union-attr]
    with pytest.raises(pytest.skip.Exception):
        async with conformance_order_guard(_sim(), _paper(), environ={}, units=_quiet, processes=_quiet):
            pass


def test_runtime_process_probe_recognises_runtime_command_lines(tmp_path: Path) -> None:
    from .conftest import running_tradepulse_runtimes

    def process(pid: int, *argv: str) -> None:
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    process(101, "/home/u/.venv/bin/python3", "/home/u/.venv/bin/tradepulse", "run", "--resume")
    process(102, "python", "-m", "tradepulse", "scan", "--asset-class", "equity")
    process(103, "python", "scripts/run_accounting_soak.py", "--hours", "24")
    process(104, "/home/u/.venv/bin/tradepulse", "status")  # places no orders
    process(105, "python", "-m", "pytest", "python_tests/test_cli.py")
    (tmp_path / "self").mkdir()
    found = running_tradepulse_runtimes(tmp_path)
    assert [line.split(":")[0] for line in sorted(found)] == ["101", "102", "103"]


# --- order cleanup (audit TP137-01) ------------------------------------------------------


def _ledger_venue() -> Venue:
    clock = ManualClock(T0)
    sim = SimulatedBroker(clock=clock, session=SimSession.from_exchange_calendar(
        "XNYS", date(2026, 10, 1), date(2026, 10, 31)))
    sim.set_quote("SPY", AssetClass.EQUITY, Decimal("600.00"), Decimal("600.05"), at=T0)
    return Venue("simulated_xnys", sim, sim, clock)


def _spy(client_order_id: str, side: Side = Side.BUY) -> OrderRequest:
    return OrderRequest(symbol="SPY", qty=Decimal("1"), side=side, client_order_id=client_order_id)


async def test_cleanup_cancels_an_order_left_open_by_an_interrupted_test() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [(timedelta(minutes=5), Decimal("1"), None)])  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="interrupted"):
        try:
            await ledger.place(_spy("c1"))  # accepted, fill still five minutes away
            raise RuntimeError("interrupted after acceptance")
        finally:
            await ledger.cleanup()
    order = await venue.broker.get_order_by_client_order_id("c1")
    assert order is not None and order.status == "canceled" and order.filled_qty == 0
    assert await venue.broker.get_positions() == []
    assert len(venue.sim._orders) == 1  # type: ignore[union-attr]  # nothing offsetting was sent


async def test_cleanup_cancels_an_order_that_never_fills() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [], terminal="expired")  # type: ignore[union-attr]  # open until the close
    placed = await ledger.place(_spy("c1"))
    with pytest.raises(AssertionError, match="not terminal"):
        await _wait_terminal(venue, placed.broker_order_id)  # the fill timeout
    await ledger.cleanup()
    assert (await venue.broker.get_order(placed.broker_order_id)).status == "canceled"


async def test_cleanup_after_a_partial_fill_reports_the_exposure_and_never_offsets_it() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [(timedelta(seconds=1), Decimal("0.4"), None),  # type: ignore[union-attr]
                                    (timedelta(minutes=5), Decimal("0.6"), None)])
    await ledger.place(_spy("c1"))
    venue.clock.advance(timedelta(seconds=2))  # type: ignore[union-attr]
    with pytest.raises(ConformanceCleanupFailed, match=r"SPY position is 0.4, it was 0 .*exposure of 0.4"):
        await ledger.cleanup()
    order = await venue.broker.get_order_by_client_order_id("c1")
    assert order is not None and (order.status, order.filled_qty) == ("canceled", Decimal("0.4"))
    assert len(venue.sim._orders) == 1  # type: ignore[union-attr]  # no flattening sell


async def test_cleanup_reports_a_cancel_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [(timedelta(minutes=5), Decimal("1"), None)])  # type: ignore[union-attr]
    await ledger.place(_spy("c1"))

    async def refuse(broker_order_id: str) -> None:
        raise AlpacaError("internal error", 500, None, None, "cancelOrder")

    monkeypatch.setattr(venue.sim, "cancel_order", refuse)
    with pytest.raises(ConformanceCleanupFailed, match=r"order c1 .*new.*cancel failed"):
        await ledger.cleanup()
    assert len(venue.sim._orders) == 1  # type: ignore[union-attr]


async def test_cleanup_reports_an_order_whose_state_cannot_be_looked_up() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [(timedelta(minutes=5), Decimal("1"), None)])  # type: ignore[union-attr]
    await ledger.place(_spy("c1"))
    venue.sim.inject(Fault.LOOKUP_TIMEOUT)  # type: ignore[union-attr]
    with pytest.raises(ConformanceCleanupFailed, match=r"order c1: lookup failed .*state unknown"):
        await ledger.cleanup()


async def test_cleanup_tracks_an_ambiguous_submission_by_client_order_id() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    venue.sim.schedule_fills("c1", [(timedelta(minutes=5), Decimal("1"), None)])  # type: ignore[union-attr]
    venue.sim.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)  # type: ignore[union-attr]
    with pytest.raises(httpx.ConnectError):
        await ledger.place(_spy("c1"))
    await ledger.cleanup()  # found by client order id and cancelled
    order = await venue.broker.get_order_by_client_order_id("c1")
    assert order is not None and order.status == "canceled"


async def test_a_completed_round_trip_cleans_up_quietly() -> None:
    venue = _ledger_venue()
    ledger = OrderLedger(venue)
    for side in (Side.BUY, Side.SELL):
        placed = await ledger.place(_spy(f"rt-{side.value}", side))
        await _wait_terminal(venue, placed.broker_order_id)
    await ledger.cleanup()


# --- process inspection (audit TP137-02) ----------------------------------------------------


def test_runtime_probe_fails_closed_when_a_process_cannot_be_inspected(tmp_path: Path) -> None:
    from .conftest import running_tradepulse_runtimes

    (tmp_path / "201").mkdir()
    unreadable = tmp_path / "201" / "cmdline"
    unreadable.write_bytes(b"python\0")
    unreadable.chmod(0)
    (tmp_path / "202").mkdir()  # exited while the probe looked: no cmdline at all
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("running with privileges that read any file")
        found = running_tradepulse_runtimes(tmp_path)
    finally:
        unreadable.chmod(0o600)
    assert len(found) == 1 and found[0].startswith("201: (command line unreadable")
    [unlisted] = running_tradepulse_runtimes(tmp_path / "missing")
    assert unlisted.startswith(f"(cannot list {tmp_path / 'missing'}: ")


async def test_order_guard_refuses_when_process_inspection_is_incomplete() -> None:
    with pytest.raises(ConformanceRefused, match="runtimes are running.*unreadable"):
        async with conformance_order_guard(_sim(), _paper(), environ=ALLOWED, units=_quiet,
                                           processes=lambda: ["201: (command line unreadable: denied)"]):
            raise AssertionError("admitted with an incomplete process inspection")
