"""Clock-calendar disagreement (Rev.144 §3.5, session sync).

In memory, per process, fed by session sync on every broker-clock read.
Disagreement means:
- the broker clock says open while the calendar has no regular session now;
- or the clock says closed for more than 5 minutes inside a calendar session.
While it lasts, new stock and option entries are refused
(CALENDAR_CLOCK_DISAGREEMENT). It clears when the two agree again.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import CalendarHorizonExceeded, MarketCalendar, Venue, ny_date

GRACE = timedelta(minutes=5)


class ClockCalendarMonitor:
    def __init__(self) -> None:
        self.active = False
        self.episode_started_at: datetime | None = None
        self._closed_since: datetime | None = None
        self._episode_days: set[date] = set()

    def observe(self, calendar: MarketCalendar, clock_is_open: bool, now: datetime) -> bool:
        """Record one clock reading. True when this reading starts a new episode."""
        try:
            in_session = calendar.is_regular_hours(Venue.US_EQUITY, now)
        except CalendarHorizonExceeded:
            in_session = False  # entries are refused beyond the horizon anyway
        if clock_is_open and not in_session:
            self._closed_since = None
            disagree = True
        elif not clock_is_open and in_session:
            self._closed_since = self._closed_since or now
            disagree = now - self._closed_since > GRACE
        else:
            self._closed_since = None
            disagree = False
        started = disagree and not self.active
        self.active = disagree
        if started:
            self.episode_started_at = now
        if disagree:
            self._episode_days.add(ny_date(now))
        return started

    def episode_on(self, day: date) -> bool:
        return day in self._episode_days

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


MONITOR = ClockCalendarMonitor()
