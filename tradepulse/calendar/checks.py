"""The daily calendar cross-check and the status it confers (Rev.144 §3.4, §3.4a).

A check is immutable events in ``calendar_check_events``: ``started``
(committed before the broker call), ``fetched`` (the raw response and its
SHA-256), then exactly one of ``completed`` (in one transaction with its
``calendar_check_dates`` rows) or ``failed``. Only completed checks confer
anything. Status is derived from those rows on every question, never stored.

Coverage: a check covers the dates from the first to the last session in
the broker's response. Within that span a library session the broker
omits, or a broker session the library lacks, is contested; a date past it
is not recorded, so it stays unverified (CAL-I6).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter
from tradepulse.models import AssetIdentity, AuditEvent
from tradepulse.persistence import PersistenceRepositories
from tradepulse.persistence.codec import decode_payload, encode_payload

from . import BrokerSession, CalendarHorizonExceeded, MarketCalendar, Venue, ny_date

Status = Literal["verified", "contested", "unverified", "beyond_horizon"]
CHECK_DAYS = 30
FRESH_SESSIONS = 3
INTERRUPTED_AFTER = timedelta(minutes=10)
CHECKED_VENUES = (Venue.US_EQUITY, Venue.US_OPTION)


def _database(repositories: PersistenceRepositories) -> Any:
    return repositories.audit_events.database


def _event(connection: sqlite3.Connection, check_id: str, event: str, broker: str, venue: Venue,
           payload: dict[str, Any], at: datetime) -> None:
    connection.execute(
        "INSERT INTO calendar_check_events (record_id,check_id,event,broker,venue,payload,created_at) VALUES (?,?,?,?,?,?,?)",
        (f"{check_id}|{event}", check_id, event, broker, venue.value, encode_payload(payload), at.isoformat()))


def _session_payload(session: BrokerSession) -> dict[str, str]:
    return {"venue": session.venue.value, "date": session.date.isoformat(), "open": session.open.isoformat(),
            "close": session.close.isoformat(), "source": session.source}


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    outcome: Literal["completed", "failed"]
    contested: tuple[date, ...] = ()
    reason: str | None = None


def compare(calendar: MarketCalendar, venue: Venue, sessions: Sequence[BrokerSession]) -> list[dict[str, Any]]:
    """One result per date in the response's coverage."""
    mine = sorted((s for s in sessions if s.venue == venue), key=lambda s: s.date)
    if not mine:
        return []
    by_day = {s.date: s for s in mine}
    if len(by_day) != len(mine):
        raise ValueError(f"CALENDAR_RESPONSE_DUPLICATE_DATE: {venue.value}")
    rows = []
    day = mine[0].date
    while day <= mine[-1].date:
        try:
            library = calendar.session(venue, day)
        except CalendarHorizonExceeded:
            break  # nothing past the library window is verified
        broker = by_day.get(day)
        agreed = (library is None and broker is None) or (
            library is not None and broker is not None and (library.open, library.close) == (broker.open, broker.close))
        rows.append({"date": day, "result": "agreed" if agreed else "contested",
                     "broker_open": broker.open.isoformat() if broker else None,
                     "broker_close": broker.close.isoformat() if broker else None,
                     "calendar_open": library.open.isoformat() if library else None,
                     "calendar_close": library.close.isoformat() if library else None})
        day += timedelta(days=1)
    return rows


async def run_check(repositories: PersistenceRepositories, broker: Any, broker_name: str, venue: Venue,
                    calendar: MarketCalendar, alerts: TelegramAlerter, now: datetime, *, clock: Any = None) -> CheckResult:
    """One check of one broker and venue, through the full lifecycle."""
    database = _database(repositories)
    check_id = str(uuid4())
    start = ny_date(now)
    end = start + timedelta(days=CHECK_DAYS)
    base = {"check_id": check_id, "broker": broker_name, "venue": venue.value,
            "range_start": start.isoformat(), "range_end": end.isoformat()}
    await database.run(lambda c: _event(c, check_id, "started", broker_name, venue, {**base, "started_at": now.isoformat()}, now),
                       write=True)
    try:
        sessions = await broker.get_calendar(start, end)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, none verifies anything
        error = repr(exc)
        await database.run(lambda c: _event(c, check_id, "failed", broker_name, venue,
                                            {**base, "reason": "fetch_failed", "error": error}, now), write=True)
        return CheckResult(check_id, "failed", reason="fetch_failed")
    received_at = clock() if clock is not None else now
    response = [_session_payload(s) for s in sessions]
    response_sha256 = sha256(encode_payload(response).encode()).hexdigest()
    await database.run(lambda c: _event(c, check_id, "fetched", broker_name, venue,
                                        {**base, "received_at": received_at.isoformat(), "response_sha256": response_sha256,
                                         "response": response}, received_at), write=True)
    try:
        rows = compare(calendar, venue, sessions)
    except ValueError as exc:
        invalid = str(exc)
        await database.run(lambda c: _event(c, check_id, "failed", broker_name, venue,
                                            {**base, "reason": "response_invalid", "error": invalid}, received_at), write=True)
        return CheckResult(check_id, "failed", reason="response_invalid")

    def complete(connection: sqlite3.Connection) -> None:
        _event(connection, check_id, "completed", broker_name, venue,
               {**base, "received_at": received_at.isoformat(), "response_sha256": response_sha256, "dates": len(rows)}, received_at)
        for row in rows:
            day = row["date"].isoformat()
            connection.execute(
                "INSERT INTO calendar_check_dates (record_id,check_id,broker,venue,date,result,checked_at,payload,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{check_id}|{broker_name}|{venue.value}|{day}", check_id, broker_name, venue.value, day, row["result"],
                 received_at.isoformat(), encode_payload({**row, "date": day}), received_at.isoformat()))

    await database.run(complete, write=True)
    contested = tuple(row["date"] for row in rows if row["result"] == "contested")
    for day in contested:
        row = next(r for r in rows if r["date"] == day)
        await _alert(repositories, alerts, f"calendar_contested:{day.isoformat()}:{venue.value}:{broker_name}",
                     "CALENDAR_CONTESTED",
                     f"CALENDAR_CONTESTED: {broker_name} {venue.value} {day.isoformat()}: broker "
                     f"{row['broker_open']}..{row['broker_close']}, calendar {row['calendar_open']}..{row['calendar_close']}. "
                     "New entries on that date are blocked.", received_at, {**row, "date": day.isoformat()})
    return CheckResult(check_id, "completed", contested)


async def record_instrument_hours(repositories: PersistenceRepositories, broker_name: str, instrument: AssetIdentity,
                                  sessions: Sequence[BrokerSession], now: datetime) -> str:
    """§3.4a: the executing broker's own contract hours for this instrument,
    as one completed check. Only the dates in ``sessions`` get rows."""
    from tradepulse.models import asset_identity_key

    database = _database(repositories)
    check_id = str(uuid4())
    key = asset_identity_key(instrument)
    response = [_session_payload(s) for s in sessions]
    digest = sha256(encode_payload(response).encode()).hexdigest()
    venue = Venue.US_OPTION
    base = {"check_id": check_id, "broker": broker_name, "venue": venue.value, "instrument_key": key}

    def write(connection: sqlite3.Connection) -> None:
        _event(connection, check_id, "started", broker_name, venue, {**base, "started_at": now.isoformat()}, now)
        _event(connection, check_id, "fetched", broker_name, venue,
               {**base, "received_at": now.isoformat(), "response_sha256": digest, "response": response}, now)
        _event(connection, check_id, "completed", broker_name, venue, {**base, "received_at": now.isoformat(),
                                                                      "response_sha256": digest}, now)
        for session in sessions:
            day = session.date.isoformat()
            connection.execute(
                "INSERT INTO instrument_hours (record_id,check_id,broker,instrument_key,date,checked_at,payload,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"{check_id}|{broker_name}|{key}|{day}", check_id, broker_name, key, day, now.isoformat(),
                 encode_payload({"open": session.open.isoformat(), "close": session.close.isoformat(),
                                 "response_sha256": digest, "source": session.source}), now.isoformat()))

    await database.run(write, write=True)
    return check_id


async def close_interrupted(repositories: PersistenceRepositories, now: datetime) -> list[str]:
    """§3.4: a check with no terminal event for more than 10 minutes gets
    ``failed``/``interrupted``. It never gets ``completed``."""
    def close(connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT check_id, broker, venue, payload, created_at FROM calendar_check_events e WHERE event='started' "
            "AND NOT EXISTS (SELECT 1 FROM calendar_check_events t WHERE t.check_id=e.check_id AND t.event IN ('completed','failed'))"
        ).fetchall()
        closed = []
        for check_id, broker, venue, payload, created_at in rows:
            if now - datetime.fromisoformat(created_at) > INTERRUPTED_AFTER:
                _event(connection, check_id, "failed", broker, Venue(venue), {**decode_payload(payload), "reason": "interrupted"}, now)
                closed.append(check_id)
        return closed

    return await _database(repositories).run(close, write=True)


async def checked_today(repositories: PersistenceRepositories, broker_name: str, venue: Venue, now: datetime) -> bool:
    today = ny_date(now).isoformat()

    def find(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM calendar_check_events WHERE event='completed' AND broker=? AND venue=? "
            "AND json_extract(payload,'$.range_start')=? AND json_extract(payload,'$.instrument_key') IS NULL LIMIT 1",
            (broker_name, venue.value, today)).fetchone() is not None

    return await _database(repositories).run(find)


async def run_daily_checks(repositories: PersistenceRepositories, broker: Any, broker_name: str, calendar: MarketCalendar,
                           alerts: TelegramAlerter, now: datetime) -> list[CheckResult]:
    """At startup and at the first reconciliation cycle of each NY date: one
    check per venue the broker serves, unless one already completed today.
    Then the unverified and horizon alerts (once per day)."""
    await close_interrupted(repositories, now)
    results = []
    for venue in CHECKED_VENUES:
        if not await checked_today(repositories, broker_name, venue, now):
            results.append(await run_check(repositories, broker, broker_name, venue, calendar, alerts, now))
    gate = CalendarGate(repositories, calendar, (broker_name,))
    day = ny_date(now).isoformat()
    try:
        upcoming = [calendar.nth_session_after(Venue.US_EQUITY, now - timedelta(days=1), n).date for n in range(1, 6)]
    except CalendarHorizonExceeded:
        await _alert(repositories, alerts, f"calendar_horizon_exceeded:{day}", "CALENDAR_HORIZON_EXCEEDED",
                     "CALENDAR_HORIZON_EXCEEDED: the next 5 sessions reach past the calendar library's window.", now, {})
        return results
    unverified = [d.isoformat() for venue in CHECKED_VENUES for d in upcoming
                  if await gate.status(venue, d, now) == "unverified"]
    if unverified:
        await _alert(repositories, alerts, f"calendar_unverified:{day}", "CALENDAR_UNVERIFIED",
                     f"CALENDAR_UNVERIFIED: {broker_name} has not freshly confirmed {sorted(set(unverified))}. "
                     "New stock and option entries on those dates are blocked.", now, {"dates": sorted(set(unverified))})
    return results


async def _alert(repositories: PersistenceRepositories, alerts: TelegramAlerter, event_id: str, event_type: str,
                 message: str, at: datetime, details: dict[str, Any]) -> None:
    from tradepulse.alerts.once import alert_once

    await alert_once(repositories.audit_events, alerts, AuditEvent(
        event_id=event_id, event_type=event_type.lower(), severity="critical", message=message, occurred_at=at,
        entity_type="market_calendar", entity_id=event_id, details=details))


class CalendarGate:
    """The calendar plus its evidence: the status of a (venue, date), and
    whether an instrument's own verified hours are valid (§3.4, §3.4a)."""

    def __init__(self, repositories: PersistenceRepositories, calendar: MarketCalendar, brokers: Sequence[str]) -> None:
        self.repositories = repositories
        self.calendar = calendar
        self.brokers = tuple(brokers)

    def fresh(self, checked_at: datetime, now: datetime) -> bool:
        """Within the last 3 NYSE sessions: at most 3 sessions dated after the check's NY date, through today."""
        return ny_date(checked_at) <= ny_date(now) and \
            self.calendar.sessions_after_count(ny_date(checked_at), ny_date(now)) <= FRESH_SESSIONS

    async def status(self, venue: Venue, day: date, now: datetime) -> Status:
        if venue == Venue.CRYPTO:
            return "verified"
        if not self.calendar.within_horizon(day):
            return "beyond_horizon"
        if not self.brokers:
            return "unverified"

        def rows(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
            return connection.execute(
                "SELECT d.broker, d.result, d.checked_at FROM calendar_check_dates d "
                "WHERE d.venue=? AND d.date=? AND EXISTS (SELECT 1 FROM calendar_check_events e "
                "WHERE e.check_id=d.check_id AND e.event='completed') ORDER BY d.checked_at, d.created_at, d.record_id",
                (venue.value, day.isoformat())).fetchall()

        found = await _database(self.repositories).run(rows)
        latest: dict[str, tuple[str, datetime]] = {}
        agreed_fresh: set[str] = set()
        for broker, result, checked_at in found:
            if broker not in self.brokers:
                continue
            at = datetime.fromisoformat(checked_at)
            latest[broker] = (result, at)
            if result == "agreed" and self.fresh(at, now):
                agreed_fresh.add(broker)
        if any(result == "contested" for result, _ in latest.values()):
            return "contested"
        if all(broker in agreed_fresh and latest[broker][0] == "agreed" for broker in self.brokers):
            return "verified"
        return "unverified"

    async def instrument_close(self, broker: str, asset: AssetIdentity, t: datetime) -> datetime | None:
        """The close of the instrument's own fresh, broker-verified hours for
        t's NY date, if valid (§3.4a); otherwise None."""
        from tradepulse.models import asset_identity_key

        day = ny_date(t)
        session = self.calendar.session(Venue.US_OPTION, day) if self.calendar.within_horizon(day) else None
        if session is None or await self.status(Venue.US_OPTION, day, t) != "verified":
            return None

        def rows(connection: sqlite3.Connection) -> list[tuple[str, str]]:
            return connection.execute(
                "SELECT h.payload, h.checked_at FROM instrument_hours h WHERE h.broker=? AND h.instrument_key=? AND h.date=? "
                "AND EXISTS (SELECT 1 FROM calendar_check_events e WHERE e.check_id=h.check_id AND e.event='completed') "
                "ORDER BY h.checked_at DESC, h.created_at DESC LIMIT 1",
                (broker, asset_identity_key(asset), day.isoformat())).fetchall()

        found = await _database(self.repositories).run(rows)
        if not found:
            return None
        payload, checked_at = found[0]
        hours = decode_payload(payload)
        close = datetime.fromisoformat(hours["close"])
        if not self.fresh(datetime.fromisoformat(checked_at), t) or close <= t or close < session.close:
            return None
        return close
