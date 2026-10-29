"""IBKR contract trading hours to BrokerSession (Rev.144 §3.3, for A2).

``reqContractDetails(...).tradingHours`` (and ``liquidHours``) is a
``;``-separated list of ``YYYYMMDD:HHMM-YYYYMMDD:HHMM`` ranges or
``YYYYMMDD:CLOSED``, in the contract's ``timeZoneId``. This follows IBKR's
documented format; A2's G0 confirms it against TWS before A2 relies on it.
A day with several ranges (a split session) is refused, not merged: no
strategy trades one, so it is not evidence of a regular session.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import BrokerSession, Venue

_ALIASES = {"US/Eastern": "America/New_York", "EST5EDT": "America/New_York", "US/Central": "America/Chicago"}


def _at(stamp: str, zone: ZoneInfo) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d:%H%M").replace(tzinfo=zone)


def parse_trading_hours(hours: str, time_zone_id: str, venue: Venue, source: str) -> list[BrokerSession]:
    zone = ZoneInfo(_ALIASES.get(time_zone_id, time_zone_id))
    sessions: dict[date, BrokerSession] = {}
    for part in filter(None, (piece.strip() for piece in hours.split(";"))):
        if part.endswith(":CLOSED"):
            day = date(int(part[:4]), int(part[4:6]), int(part[6:8]))
            if day in sessions:
                raise ValueError(f"IBKR_HOURS_INVALID: {day} both open and closed")
            continue
        start, sep, end = part.partition("-")
        if not sep:
            raise ValueError(f"IBKR_HOURS_INVALID: {part!r}")
        open_at, close_at = _at(start, zone), _at(end, zone)
        if close_at <= open_at:
            raise ValueError(f"IBKR_HOURS_INVALID: {part!r} closes before it opens")
        day = open_at.date()
        if day in sessions:
            raise ValueError(f"IBKR_HOURS_SPLIT_SESSION: {day}")
        sessions[day] = BrokerSession(venue, day, open_at.astimezone(ZoneInfo("UTC")), close_at.astimezone(ZoneInfo("UTC")), source)
    return [sessions[day] for day in sorted(sessions)]
