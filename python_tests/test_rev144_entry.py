"""Rev.144 task 4: entry authorization (spec §3.5, §3.6; T3-T6, T9-T11, Review Focus 1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from rev140_support import Rt

from tradepulse.alerts import TelegramAlerter
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue, ny_time
from tradepulse.calendar.checks import CalendarGate, entry_window_open, record_instrument_hours, run_check
from tradepulse.calendar.disagreement import MONITOR, ClockCalendarMonitor
from tradepulse.models import AssetClass, AssetIdentity
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, paginate_all_rows

CAL = MarketCalendar.from_library()
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
OPTION = AssetIdentity("SPY261218C00600000", AssetClass.OPTION, "alpaca:SPY261218C00600000", contract_multiplier=100)
OTHER_OPTION = AssetIdentity("SPY261218P00500000", AssetClass.OPTION, "alpaca:SPY261218P00500000", contract_multiplier=100)
BTC = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")


@dataclass
class Clock:
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None
    timestamp: datetime | None = None


class _Broker:
    def __init__(self, sessions: list[BrokerSession]) -> None:
        self.sessions = sessions

    async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
        return [s for s in self.sessions if start <= s.date <= end]


def _library(start: date, end: date) -> list[BrokerSession]:
    return [BrokerSession(v, s.date, s.open, s.close, "test") for v in (Venue.US_EQUITY, Venue.US_OPTION)
            for s in CAL.sessions_between(v, start, end)]


@pytest.fixture
async def repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/entry.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _verified(repositories: PersistenceRepositories, broker: str, at: datetime, *, extra: list[BrokerSession] | None = None,
                    drop: date | None = None) -> CalendarGate:
    sessions = [s for s in _library(at.date() - timedelta(days=3), at.date() + timedelta(days=30)) if s.date != drop] + (extra or [])
    for venue in (Venue.US_EQUITY, Venue.US_OPTION):
        await run_check(repositories, _Broker(sessions), broker, venue, CAL, TelegramAlerter(None, None), at)
    return CalendarGate(repositories, CAL, (broker,))


async def test_holiday_with_the_clock_open_is_refused(repositories: PersistenceRepositories) -> None:
    """T4: Thanksgiving, verified closed by the broker, with an (injected) open clock."""
    t = datetime(2026, 11, 26, 15, tzinfo=UTC)
    gate = await _verified(repositories, "alpaca", t - timedelta(days=1))
    window = await entry_window_open(gate, "alpaca", AAPL, t, Clock(True), ClockCalendarMonitor())
    assert (window.open, window.reason) == (False, "CALENDAR_SESSION_CLOSED")


async def test_regular_session_is_open_and_clock_closed_keeps_its_string(repositories: PersistenceRepositories) -> None:
    t = datetime(2026, 11, 24, 15, tzinfo=UTC)
    gate = await _verified(repositories, "alpaca", t)
    window = await entry_window_open(gate, "alpaca", AAPL, t, Clock(True), ClockCalendarMonitor())
    assert (window.open, window.basis, window.reason) == (True, "venue_regular_session", None)
    closed = await entry_window_open(gate, "alpaca", AAPL, t, Clock(False), ClockCalendarMonitor())
    assert closed.reason == "EQUITY_MARKET_CLOSED"
    after_early_close = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)  # 13:30 ET on the day after Thanksgiving
    assert (await entry_window_open(gate, "alpaca", AAPL, after_early_close, Clock(True), ClockCalendarMonitor())).reason == \
        "CALENDAR_SESSION_CLOSED"


async def test_contested_unverified_and_beyond_horizon(repositories: PersistenceRepositories) -> None:
    """T3 (gateway reason), T5 and T6 at the decision."""
    t = datetime(2026, 11, 24, 15, tzinfo=UTC)
    # checked the day before; the broker's response omits 11-24 inside its span: contested
    gate = await _verified(repositories, "alpaca", t - timedelta(days=1), drop=date(2026, 11, 24))
    assert (await entry_window_open(gate, "alpaca", AAPL, t, Clock(True), ClockCalendarMonitor())).reason == "CALENDAR_DATE_CONTESTED"
    stale = datetime(2026, 12, 1, 15, tzinfo=UTC)  # four sessions after the last check
    fresh_gate = await _verified(repositories, "ibkr", t)
    assert (await entry_window_open(fresh_gate, "ibkr", AAPL, stale, Clock(True), ClockCalendarMonitor())).reason == "CALENDAR_UNVERIFIED"
    beyond = datetime.combine(CAL.last + timedelta(days=2), time(15), UTC)
    assert (await entry_window_open(fresh_gate, "ibkr", AAPL, beyond, Clock(True), ClockCalendarMonitor())).reason == \
        "CALENDAR_HORIZON_EXCEEDED"


async def test_crypto_is_never_gated(repositories: PersistenceRepositories) -> None:
    """T11: crypto entries are open on a holiday, unverified, and in a disagreement."""
    monitor = ClockCalendarMonitor()
    monitor.observe(CAL, True, datetime(2026, 11, 26, 15, tzinfo=UTC))
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    window = await entry_window_open(gate, "alpaca", BTC, datetime(2026, 11, 26, 15, tzinfo=UTC), Clock(False), monitor)
    assert (window.open, window.basis) == (True, "continuous")


def test_disagreement_grace_and_clearing() -> None:
    """T9: clock closed inside a session for 4 minutes -> no block; 6 -> block, once; agreement clears."""
    monitor = ClockCalendarMonitor()
    start = datetime(2026, 11, 24, 15, tzinfo=UTC)
    assert not monitor.observe(CAL, False, start)
    assert not monitor.observe(CAL, False, start + timedelta(minutes=4)) and not monitor.active
    assert monitor.observe(CAL, False, start + timedelta(minutes=6)) and monitor.active
    assert not monitor.observe(CAL, False, start + timedelta(minutes=7)) and monitor.active  # same episode
    assert not monitor.observe(CAL, True, start + timedelta(minutes=8)) and not monitor.active
    assert monitor.episode_on(date(2026, 11, 24))
    assert monitor.observe(CAL, True, datetime(2026, 11, 26, 15, tzinfo=UTC))  # open on a holiday: immediately


async def test_disagreement_blocks_entries_and_alerts_once_through_session_sync(repositories: PersistenceRepositories) -> None:
    """T9 through risk/session.py::sync_market_session."""
    from tradepulse.risk.session import sync_market_session

    class ClosedClock:
        venue = "alpaca"

        async def get_clock(self) -> Clock:
            return Clock(False)

    start = datetime(2026, 11, 24, 15, tzinfo=UTC)
    for minutes in (0, 6, 7):
        await sync_market_session(repositories, ClosedClock(), lambda m=minutes: start + timedelta(minutes=m))  # type: ignore[arg-type]
    alerts = [r for r in await paginate_all_rows(repositories.audit_events) if r["payload"]["event_type"] == "calendar_clock_disagreement"]
    assert len(alerts) == 1
    gate = await _verified(repositories, "alpaca", start)
    assert (await entry_window_open(gate, "alpaca", AAPL, start + timedelta(minutes=7), Clock(True))).reason == \
        "CALENDAR_CLOCK_DISAGREEMENT"


async def test_option_late_close(repositories: PersistenceRepositories) -> None:
    """T10: 16:05 ET with only the venue calendar is refused; the instrument's
    own fresh 16:15 row from the executing broker opens it, until 16:15;
    another option on the same underlying, another broker's row, or a
    reference contract's row does not."""
    day = date(2026, 11, 24)
    t = ny_time(day, time(16, 5))
    clock = Clock(False, next_open=ny_time(date(2026, 11, 25), time(9, 30)))
    gate = await _verified(repositories, "ibkr", t)
    assert (await entry_window_open(gate, "ibkr", OPTION, t, clock, ClockCalendarMonitor())).reason == "OPTION_INSTRUMENT_HOURS_UNVERIFIED"
    late = [BrokerSession(Venue.US_OPTION, day, ny_time(day, time(9, 30)), ny_time(day, time(16, 15)), "ibkr:tradingHours:1")]
    await record_instrument_hours(repositories, "ibkr", OPTION, late, t - timedelta(hours=2))
    window = await entry_window_open(gate, "ibkr", OPTION, t, clock, ClockCalendarMonitor())
    assert (window.open, window.basis) == (True, "verified_instrument_hours")
    assert not (await entry_window_open(gate, "ibkr", OPTION, ny_time(day, time(16, 15)), clock, ClockCalendarMonitor())).open
    assert (await entry_window_open(gate, "ibkr", OTHER_OPTION, t, clock, ClockCalendarMonitor())).reason == \
        "OPTION_INSTRUMENT_HOURS_UNVERIFIED"
    other_broker = await _verified(repositories, "alpaca", t)
    assert (await entry_window_open(other_broker, "alpaca", OPTION, t, clock, ClockCalendarMonitor())).reason == \
        "OPTION_INSTRUMENT_HOURS_UNVERIFIED"  # the row is IBKR's, not Alpaca's
    open_clock = Clock(True)  # an inconsistent clock never stands in
    assert not (await entry_window_open(gate, "ibkr", OPTION, t, open_clock, ClockCalendarMonitor())).open
    monitor = ClockCalendarMonitor()
    monitor.observe(CAL, True, ny_time(day, time(8)))
    monitor.observe(CAL, False, ny_time(day, time(8, 1)))  # an episode earlier today, now cleared
    assert not (await entry_window_open(gate, "ibkr", OPTION, t, clock, monitor)).open


async def test_instrument_hours_never_create_a_session(repositories: PersistenceRepositories) -> None:
    """T10a: a row on a holiday, on a contested day, or a stale row is refused."""
    holiday = date(2026, 11, 26)
    t = ny_time(holiday, time(16, 5))
    clock = Clock(False, next_open=ny_time(date(2026, 11, 27), time(9, 30)))
    gate = await _verified(repositories, "ibkr", t - timedelta(days=1))
    await record_instrument_hours(repositories, "ibkr", OPTION, [BrokerSession(
        Venue.US_OPTION, holiday, ny_time(holiday, time(9, 30)), ny_time(holiday, time(16, 15)), "ibkr")], t)
    assert not (await entry_window_open(gate, "ibkr", OPTION, t, clock, ClockCalendarMonitor())).open
    day = date(2026, 12, 1)
    t2 = ny_time(day, time(16, 5))
    contested = await _verified(repositories, "ibkr", t2 - timedelta(days=1), drop=day)
    await record_instrument_hours(repositories, "ibkr", OPTION, [BrokerSession(
        Venue.US_OPTION, day, ny_time(day, time(9, 30)), ny_time(day, time(16, 15)), "ibkr")], t2)
    assert (await entry_window_open(contested, "ibkr", OPTION, t2, clock, ClockCalendarMonitor())).reason == "CALENDAR_DATE_CONTESTED"
    stale_day = date(2026, 12, 8)
    t3 = ny_time(stale_day, time(16, 5))
    fresh_venue = await _verified(repositories, "ibkr", t3)
    await record_instrument_hours(repositories, "ibkr", OPTION, [BrokerSession(
        Venue.US_OPTION, stale_day, ny_time(stale_day, time(9, 30)), ny_time(stale_day, time(16, 15)), "ibkr")], t3 - timedelta(days=7))
    assert (await entry_window_open(fresh_venue, "ibkr", OPTION, t3, clock, ClockCalendarMonitor())).reason == \
        "OPTION_INSTRUMENT_HOURS_UNVERIFIED"


async def test_gateway_contested_date_refuses_entries_and_submits_exits(rt: Rt) -> None:
    """T5 and T11 end to end: the rt runtime (library calendar, simulator on XNYS)."""
    assert (await rt.buy("10")).status == "filled"  # the gateway ran today's check itself (Review Focus 1)
    rt.broker.inject_calendar_difference(rt.clock().date(), close=rt.clock().replace(hour=19))
    await run_check(rt.repositories, rt.broker, "alpaca", Venue.US_EQUITY, CAL, rt.alerts, rt.clock())
    rt.quote()
    refused = await rt.buy("1")
    assert (refused.status, refused.reasons) == ("rejected", ["CALENDAR_DATE_CONTESTED"])
    rt.quote()
    exit_result = await rt.sell("10", "exit")
    assert exit_result.status == "filled"  # a protective exit never consults the calendar (CAL-I2)


async def test_gateway_unverified_when_the_broker_calendar_is_unreachable(rt: Rt, monkeypatch: pytest.MonkeyPatch) -> None:
    """T6: no successful check for more than 3 trading days -> entries refused, exits allowed."""
    assert (await rt.buy("5")).status == "filled"

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(rt.broker, "get_calendar", down)
    rt.clock.advance(timedelta(days=7))  # Monday 10-26, four sessions after the last check
    rt.quote()
    refused = await rt.buy("1")
    assert (refused.status, refused.reasons) == ("rejected", ["CALENDAR_UNVERIFIED"])
    rt.quote()
    assert (await rt.sell("5", "exit")).status == "filled"
    alerts = [r for r in await paginate_all_rows(rt.repositories.audit_events) if r["payload"]["event_type"] == "calendar_unverified"]
    assert len(alerts) == 1


def test_the_process_monitor_is_reset_between_tests() -> None:
    assert not MONITOR.active

