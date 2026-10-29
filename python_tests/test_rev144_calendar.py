"""Rev.144 task 1: the pinned venue calendar (spec §3.1, §3.2; T1-T3, T15)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from tradepulse.calendar import CalendarHorizonExceeded, MarketCalendar, Venue, market_calendar, ny_time


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    return MarketCalendar.from_library()


def test_library_sessions(calendar: MarketCalendar) -> None:
    """T1: Thanksgiving, the day after (13:00), Christmas Eve (13:00), observed July 4th."""
    assert not calendar.is_session(Venue.US_EQUITY, date(2026, 11, 26))
    friday = calendar.session(Venue.US_EQUITY, date(2026, 11, 27))
    assert friday is not None and friday.early_close and friday.close == ny_time(date(2026, 11, 27), time(13))
    eve = calendar.session(Venue.US_EQUITY, date(2026, 12, 24))
    assert eve is not None and eve.close == ny_time(date(2026, 12, 24), time(13))
    assert not calendar.is_session(Venue.US_EQUITY, date(2027, 7, 5))
    ordinary = calendar.session(Venue.US_EQUITY, date(2026, 10, 29))
    assert ordinary is not None and not ordinary.early_close
    assert (ordinary.open, ordinary.close) == (datetime(2026, 10, 29, 13, 30, tzinfo=UTC), datetime(2026, 10, 29, 20, tzinfo=UTC))


def test_option_venue_uses_session_days_with_a_fixed_close(calendar: MarketCalendar) -> None:
    option = calendar.session(Venue.US_OPTION, date(2026, 11, 2))  # after the DST change
    assert option is not None and option.close == datetime(2026, 11, 2, 21, tzinfo=UTC)
    early = calendar.session(Venue.US_OPTION, date(2026, 11, 27))
    assert early is not None and early.close == ny_time(date(2026, 11, 27), time(13))
    assert calendar.session(Venue.US_OPTION, date(2026, 11, 26)) is None


def test_nth_session_after(calendar: MarketCalendar) -> None:
    """T2: Wednesday 2026-11-25, n=3 -> Tuesday 12-01; the early close counts."""
    t = datetime(2026, 11, 25, 15, tzinfo=UTC)
    assert calendar.nth_session_after(Venue.US_EQUITY, t, 1).date == date(2026, 11, 27)
    assert calendar.nth_session_after(Venue.US_EQUITY, t, 3).date == date(2026, 12, 1)
    late = datetime(2026, 11, 26, 4, 30, tzinfo=UTC)  # 23:30 NY on the 25th
    assert calendar.nth_session_after(Venue.US_EQUITY, late, 1).date == date(2026, 11, 27)


def test_horizon(calendar: MarketCalendar) -> None:
    """T3 (calendar part): past the library window raises; nothing extrapolated."""
    beyond = calendar.last + timedelta(days=1)
    assert not calendar.within_horizon(beyond)
    with pytest.raises(CalendarHorizonExceeded):
        calendar.is_session(Venue.US_EQUITY, beyond)
    with pytest.raises(CalendarHorizonExceeded):
        calendar.nth_session_after(Venue.US_EQUITY, datetime.combine(calendar.last, time(12), UTC), 1)
    assert calendar.is_session(Venue.CRYPTO, beyond)  # crypto is always open


def test_regular_hours_and_crypto(calendar: MarketCalendar) -> None:
    assert calendar.is_regular_hours(Venue.US_EQUITY, datetime(2026, 10, 29, 14, tzinfo=UTC))
    assert not calendar.is_regular_hours(Venue.US_EQUITY, datetime(2026, 10, 29, 20, tzinfo=UTC))  # close exclusive
    assert not calendar.is_regular_hours(Venue.US_EQUITY, datetime(2026, 11, 27, 18, 30, tzinfo=UTC))  # after 13:00
    assert calendar.is_regular_hours(Venue.CRYPTO, datetime(2026, 11, 26, 3, tzinfo=UTC))


def test_sessions_between_is_inclusive(calendar: MarketCalendar) -> None:
    days = [s.date for s in calendar.sessions_between(Venue.US_EQUITY, date(2026, 11, 25), date(2026, 11, 30))]
    assert days == [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    assert calendar.sessions_after_count(date(2026, 11, 25), date(2026, 11, 30)) == 2


def test_fingerprint_is_stable_per_anchor_and_pinned(calendar: MarketCalendar) -> None:
    anchor = date(2026, 10, 29)
    assert calendar.fingerprint(anchor) == MarketCalendar.from_library().fingerprint(anchor)
    assert calendar.fingerprint(anchor) != calendar.fingerprint(anchor + timedelta(days=1))
    assert market_calendar(anchor) is market_calendar(anchor)


def test_day_counting_is_unchanged() -> None:
    """T15: the time stop, stall exit and risk day keep calendar days and NY midnight."""
    from tradepulse.risk.engine import _risk_day_bounds

    start, end = _risk_day_bounds(datetime(2026, 11, 26, 15, tzinfo=UTC))  # a holiday is still a risk day
    assert end - start == timedelta(days=1)
