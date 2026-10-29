"""Rev.144 task 2: get_calendar on the broker port (spec §3.3; T13)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient
from tradepulse.broker.errors import AlpacaDataIntegrityError
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue
from tradepulse.calendar.ibkr_hours import parse_trading_hours

FIXTURE = Path(__file__).parent / "fixtures" / "rev144" / "alpaca_calendar.json"
URL = "https://paper-api.alpaca.markets/v2/calendar"


@respx.mock
async def test_alpaca_calendar_from_the_recorded_g0_response() -> None:
    """T13 (Alpaca): NY-local HH:MM to UTC across the DST change; early
    closes kept; one session per US venue; and it agrees with the library."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    sessions = await AlpacaClient("key", "secret", "paper", 10).get_calendar(date(2026, 10, 27), date(2027, 1, 31))
    assert route.calls.last.request.url.params["start"] == "2026-10-27"
    by_day = {(s.venue, s.date): s for s in sessions}
    assert by_day[(Venue.US_EQUITY, date(2026, 10, 30))].open == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)  # EDT
    assert by_day[(Venue.US_EQUITY, date(2026, 11, 2))].open == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)  # EST
    assert by_day[(Venue.US_OPTION, date(2026, 11, 27))].close == datetime(2026, 11, 27, 18, tzinfo=UTC)  # 13:00 ET
    assert (Venue.US_EQUITY, date(2026, 11, 26)) not in by_day
    assert all(isinstance(s, BrokerSession) and s.source == "alpaca:/v2/calendar" for s in sessions)
    calendar = MarketCalendar.from_library()
    for s in sessions:
        library = calendar.session(s.venue, s.date)
        assert library is not None and (library.open, library.close) == (s.open, s.close)
    assert len(json.loads(FIXTURE.read_text())) * 2 == len(sessions)


@respx.mock
async def test_alpaca_calendar_refuses_an_unparsable_row() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=[{"date": "2026-10-29", "open": "9:30am", "close": "16:00"}]))
    with pytest.raises(AlpacaDataIntegrityError):
        await AlpacaClient("key", "secret", "paper", 10).get_calendar(date(2026, 10, 29), date(2026, 10, 29))


async def test_simulator_calendar_and_injected_differences() -> None:
    """T13 (simulator) plus the hooks T7 and T8 use."""
    broker = SimulatedBroker(clock=ManualClock(datetime(2026, 10, 29, 14, tzinfo=UTC)),
                             session=SimSession.from_exchange_calendar("XNYS", date(2026, 10, 1), date(2026, 12, 31)))
    sessions = await broker.get_calendar(date(2026, 11, 25), date(2026, 11, 30))
    assert sorted({s.date for s in sessions}) == [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    assert {s.venue for s in sessions} == {Venue.US_EQUITY, Venue.US_OPTION}
    broker.inject_calendar_difference(date(2026, 11, 27), holiday=True)
    broker.inject_calendar_difference(date(2026, 11, 30), close=datetime(2026, 11, 30, 20, 30, tzinfo=UTC))
    changed = {s.date: s for s in await broker.get_calendar(date(2026, 11, 25), date(2026, 11, 30)) if s.venue == Venue.US_EQUITY}
    assert date(2026, 11, 27) not in changed and changed[date(2026, 11, 30)].close == datetime(2026, 11, 30, 20, 30, tzinfo=UTC)
    always = SimulatedBroker(clock=ManualClock(datetime(2026, 10, 29, tzinfo=UTC)), session=SimSession.always_open())
    assert await always.get_calendar(date(2026, 10, 1), date(2026, 12, 31)) == []


def test_ibkr_trading_hours_string() -> None:
    """T13 (IBKR hours-string fixture, IBKR's documented format)."""
    hours = ("20261125:0930-20261125:1600;20261126:CLOSED;20261127:0930-20261127:1300;"
             "20261128:CLOSED;20261129:CLOSED;20261130:0930-20261130:1600")
    sessions = parse_trading_hours(hours, "US/Eastern", Venue.US_EQUITY, "ibkr:tradingHours:756733")
    assert [(s.date, s.open, s.close) for s in sessions] == [
        (date(2026, 11, 25), datetime(2026, 11, 25, 14, 30, tzinfo=UTC), datetime(2026, 11, 25, 21, tzinfo=UTC)),
        (date(2026, 11, 27), datetime(2026, 11, 27, 14, 30, tzinfo=UTC), datetime(2026, 11, 27, 18, tzinfo=UTC)),
        (date(2026, 11, 30), datetime(2026, 11, 30, 14, 30, tzinfo=UTC), datetime(2026, 11, 30, 21, tzinfo=UTC))]
    with pytest.raises(ValueError, match="SPLIT_SESSION"):
        parse_trading_hours("20261130:0930-20261130:1200;20261130:1300-20261130:1600", "US/Eastern", Venue.US_EQUITY, "x")
