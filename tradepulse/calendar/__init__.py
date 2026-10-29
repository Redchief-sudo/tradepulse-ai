"""Shared market calendar, keyed by venue (Rev.144, spec §3.1, §3.2).

The truth is the pinned ``exchange_calendars`` library: NYSE (XNYS) for US
equities, and its session days with a 16:00 ET close (13:00 on early-close
days) for US options. Crypto is always open. Built from the library's
default window (20 years back, 1 year ahead) and never extrapolated: a
date past it raises CalendarHorizonExceeded. Rebuilt once per New York date.

This module is pure. Whether a date is verified by every connected broker
is evidence in the database, answered by ``checks.CalendarGate``.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from zoneinfo import ZoneInfo

from tradepulse.models import AssetClass, AssetIdentity

NEW_YORK = ZoneInfo("America/New_York")
LIBRARY = "exchange_calendars"
LIBRARY_VERSION = "4.13.2"
OPTION_CLOSE = time(16, 0)
OPTION_EARLY_CLOSE = time(13, 0)


class Venue(StrEnum):
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"


class CalendarHorizonExceeded(ValueError):
    """A date outside the library's window: never extrapolated (fail closed)."""


@dataclass(frozen=True, slots=True)
class Session:
    venue: Venue
    date: date  # the trading date, America/New_York
    open: datetime  # aware UTC
    close: datetime  # aware UTC; early closes included
    early_close: bool


@dataclass(frozen=True, slots=True)
class BrokerSession:
    """One session as a broker reports it (§3.3)."""

    venue: Venue
    date: date
    open: datetime  # aware UTC
    close: datetime  # aware UTC
    source: str  # e.g. "alpaca:/v2/calendar"


def venue_for(asset: AssetIdentity) -> Venue:
    if asset.asset_class == AssetClass.CRYPTO:
        return Venue.CRYPTO
    return Venue.US_OPTION if asset.asset_class == AssetClass.OPTION else Venue.US_EQUITY


def ny_date(t: datetime) -> date:
    return t.astimezone(NEW_YORK).date()


def ny_time(day: date, at: time) -> datetime:
    return datetime.combine(day, at, NEW_YORK).astimezone(UTC)


class MarketCalendar:
    """Sessions for each venue from one library build."""

    def __init__(self, equity_sessions: dict[date, tuple[datetime, datetime]], *, first: date, last: date,
                 library_version: str = LIBRARY_VERSION, option_close_from_equity: bool = False) -> None:
        """``option_close_from_equity`` is for test calendars only: options
        close with the equity session instead of at 16:00 (13:00) ET."""
        self._option_close_from_equity = option_close_from_equity
        self._equity = dict(sorted(equity_sessions.items()))
        self._dates = list(self._equity)
        self.first, self.last = first, last
        self.library_version = library_version

    @classmethod
    def from_library(cls) -> MarketCalendar:
        import exchange_calendars

        if exchange_calendars.__version__ != LIBRARY_VERSION:
            raise RuntimeError(f"exchange_calendars {exchange_calendars.__version__} installed; Rev.144 pins {LIBRARY_VERSION}")
        library = exchange_calendars.get_calendar("XNYS")
        schedule = library.schedule
        sessions = {index.date(): (row.open.to_pydatetime().astimezone(UTC), row.close.to_pydatetime().astimezone(UTC))
                    for index, row in zip(schedule.index, schedule.itertuples(), strict=True)}
        return cls(sessions, first=library.first_session.date(), last=library.last_session.date(),
                   library_version=exchange_calendars.__version__)

    def _require(self, day: date) -> None:
        if day < self.first or day > self.last:
            raise CalendarHorizonExceeded(f"CALENDAR_HORIZON_EXCEEDED: {day.isoformat()} outside {self.first}..{self.last}")

    def within_horizon(self, day: date) -> bool:
        return self.first <= day <= self.last

    def is_session(self, venue: Venue, day: date) -> bool:
        if venue == Venue.CRYPTO:
            return True
        self._require(day)
        return day in self._equity

    def session(self, venue: Venue, day: date) -> Session | None:
        if venue == Venue.CRYPTO:
            start = datetime.combine(day, time(0), UTC)
            return Session(venue, day, start, start + timedelta(days=1), False)
        self._require(day)
        hours = self._equity.get(day)
        if hours is None:
            return None
        open_at, close_at = hours
        early = close_at < ny_time(day, OPTION_CLOSE)
        if venue == Venue.US_OPTION and not self._option_close_from_equity:
            close_at = ny_time(day, OPTION_EARLY_CLOSE if early else OPTION_CLOSE)
        return Session(venue, day, open_at, close_at, early)

    def sessions_between(self, venue: Venue, start: date, end: date) -> list[Session]:
        """Inclusive of both ends."""
        if venue == Venue.CRYPTO:
            return [s for s in (self.session(venue, start + timedelta(days=n)) for n in range((end - start).days + 1)) if s]
        self._require(start)
        self._require(end)
        low, high = bisect.bisect_left(self._dates, start), bisect.bisect_right(self._dates, end)
        return [s for s in (self.session(venue, day) for day in self._dates[low:high]) if s is not None]

    def nth_session_after(self, venue: Venue, t: datetime, n: int) -> Session:
        """The n-th session (n >= 1) whose date is after t's New York date."""
        if n < 1:
            raise ValueError("n must be >= 1")
        day = ny_date(t)
        if venue == Venue.CRYPTO:
            return self.session(venue, day + timedelta(days=n))  # type: ignore[return-value]
        self._require(day)
        index = bisect.bisect_right(self._dates, day) + n - 1
        if index >= len(self._dates):
            raise CalendarHorizonExceeded(f"CALENDAR_HORIZON_EXCEEDED: {n} sessions after {day.isoformat()}")
        session = self.session(venue, self._dates[index])
        assert session is not None
        return session

    def sessions_after_count(self, after: date, through: date) -> int:
        """Equity sessions with a date in (after, through]."""
        return bisect.bisect_right(self._dates, through) - bisect.bisect_right(self._dates, after)

    def is_regular_hours(self, venue: Venue, t: datetime) -> bool:
        if venue == Venue.CRYPTO:
            return True
        session = self.session(venue, ny_date(t))
        return session is not None and session.open <= t < session.close

    def fingerprint(self, anchor: date) -> str:
        """SHA-256 of the US_EQUITY sessions in [anchor, anchor + 365 days],
        clipped to the library window, plus the library version (§3.2)."""
        end = min(anchor + timedelta(days=365), self.last)
        rows = [[s.date.isoformat(), s.open.isoformat(), s.close.isoformat()]
                for s in (self.sessions_between(Venue.US_EQUITY, anchor, end) if anchor <= end else [])]
        return sha256(json.dumps({"library": LIBRARY, "version": self.library_version, "sessions": rows},
                                 separators=(",", ":")).encode()).hexdigest()


_CACHE: dict[date, MarketCalendar] = {}


def market_calendar(today: date | None = None) -> MarketCalendar:
    """The process calendar, built once per New York date."""
    key = today or ny_date(datetime.now(UTC))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = MarketCalendar.from_library()
    return _CACHE[key]
