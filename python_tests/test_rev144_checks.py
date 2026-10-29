"""Rev.144 task 3: the cross-check lifecycle and per-date status (spec §3.4, §3.7; T7, T8, T14, T17-T19)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue
from tradepulse.calendar.checks import CalendarGate, close_interrupted, run_check, run_daily_checks
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, paginate_all_rows
from tradepulse.persistence.database import CALENDAR_TABLES, DatabaseError

NOW = datetime(2026, 11, 24, 15, tzinfo=UTC)  # Tuesday before Thanksgiving, 10:00 NY
CAL = MarketCalendar.from_library()


class _Broker:
    """A broker whose calendar is a given list, or an exception."""

    def __init__(self, sessions: list[BrokerSession] | Exception) -> None:
        self.sessions = sessions

    async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
        if isinstance(self.sessions, Exception):
            raise self.sessions
        return [s for s in self.sessions if start <= s.date <= end]


def _library(start: date, end: date, venues: tuple[Venue, ...] = (Venue.US_EQUITY, Venue.US_OPTION)) -> list[BrokerSession]:
    return [BrokerSession(v, s.date, s.open, s.close, "test") for v in venues for s in CAL.sessions_between(v, start, end)]


@pytest.fixture
async def repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/calendar.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _sim() -> SimulatedBroker:
    return SimulatedBroker(clock=ManualClock(NOW), session=SimSession.from_exchange_calendar("XNYS", date(2026, 10, 1), date(2027, 1, 31)))


async def _alerts(repositories: PersistenceRepositories, kind: str) -> list[Any]:
    return [row for row in await paginate_all_rows(repositories.audit_events) if row["payload"]["event_type"] == kind]


async def test_agreeing_check_verifies_its_dates(repositories: PersistenceRepositories) -> None:
    result = await run_check(repositories, _sim(), "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None), NOW)
    assert result.outcome == "completed" and result.contested == ()
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    assert await gate.status(Venue.US_EQUITY, date(2026, 11, 24), NOW) == "verified"
    assert await gate.status(Venue.US_EQUITY, date(2026, 11, 26), NOW) == "verified"  # the holiday agreed as closed
    assert await gate.status(Venue.US_OPTION, date(2026, 11, 24), NOW) == "unverified"  # not checked for that venue


async def test_extra_holiday_is_contested_once_then_cleared(repositories: PersistenceRepositories) -> None:
    """T7: an extra broker holiday is contested with one alert; a later
    agreeing check clears it; both rows are kept."""
    broker = _sim()
    broker.inject_calendar_difference(date(2026, 11, 27), holiday=True)
    alerts = TelegramAlerter(None, None)
    first = await run_check(repositories, broker, "alpaca", Venue.US_EQUITY, CAL, alerts, NOW)
    assert first.contested == (date(2026, 11, 27),)
    await run_check(repositories, broker, "alpaca", Venue.US_EQUITY, CAL, alerts, NOW + timedelta(minutes=5))
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    assert await gate.status(Venue.US_EQUITY, date(2026, 11, 27), NOW) == "contested"
    assert len(await _alerts(repositories, "calendar_contested")) == 1
    broker.clear_calendar_differences()
    await run_check(repositories, broker, "alpaca", Venue.US_EQUITY, CAL, alerts, NOW + timedelta(hours=1))
    later = NOW + timedelta(hours=1)
    assert await gate.status(Venue.US_EQUITY, date(2026, 11, 27), later) == "verified"
    with sqlite3.connect(repositories.audit_events.database.path) as connection:
        results = [r[0] for r in connection.execute(
            "SELECT result FROM calendar_check_dates WHERE date='2026-11-27' ORDER BY checked_at")]
    assert results == ["contested", "contested", "agreed"]


async def test_shifted_close_is_contested(repositories: PersistenceRepositories) -> None:
    """T8."""
    broker = _sim()
    broker.inject_calendar_difference(date(2026, 12, 1), close=datetime(2026, 12, 1, 21, 15, tzinfo=UTC))
    result = await run_check(repositories, broker, "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None), NOW)
    assert result.contested == (date(2026, 12, 1),)
    assert await CalendarGate(repositories, CAL, ("alpaca",)).status(Venue.US_EQUITY, date(2026, 12, 1), NOW) == "contested"


@pytest.mark.parametrize("table", CALENDAR_TABLES)
async def test_tables_are_append_only(repositories: PersistenceRepositories, table: str) -> None:
    """T14 (update and delete)."""
    await run_check(repositories, _sim(), "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None), NOW)
    from tradepulse.calendar.checks import record_instrument_hours
    from tradepulse.models import AssetClass, AssetIdentity

    option = AssetIdentity("SPY261231C00600000", AssetClass.OPTION, "alpaca:SPY261231C00600000", contract_multiplier=100)
    await record_instrument_hours(repositories, "ibkr", option, _library(date(2026, 11, 24), date(2026, 11, 24), (Venue.US_OPTION,)), NOW)
    with sqlite3.connect(repositories.audit_events.database.path) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
        for statement in (f"UPDATE {table} SET payload='{{}}'", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)


async def test_a_missing_trigger_refuses_startup(tmp_path: Path) -> None:
    """T14 (startup)."""
    path = tmp_path / "t.db"
    await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER calendar_rows_need_completed_calendar_check_dates")
    with pytest.raises(DatabaseError, match="SCHEMA_TRIGGER_MISSING:calendar_rows_need_completed_calendar_check_dates"):
        await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()


async def test_identity_same_timestamp_and_overlapping_ranges(repositories: PersistenceRepositories) -> None:
    """T17: two checks at the same time for different venues, and overlapping ranges, both persist."""
    alerts = TelegramAlerter(None, None)
    a = await run_check(repositories, _sim(), "alpaca", Venue.US_EQUITY, CAL, alerts, NOW)
    b = await run_check(repositories, _sim(), "alpaca", Venue.US_OPTION, CAL, alerts, NOW)
    c = await run_check(repositories, _sim(), "alpaca", Venue.US_EQUITY, CAL, alerts, NOW)
    assert len({a.check_id, b.check_id, c.check_id}) == 3
    with sqlite3.connect(repositories.audit_events.database.path) as connection:
        counts = connection.execute("SELECT check_id, COUNT(*) FROM calendar_check_dates GROUP BY check_id").fetchall()
        assert len(counts) == 3 and len({n for _, n in counts}) == 1
        with pytest.raises(sqlite3.IntegrityError):  # replaying a check's rows changes nothing
            connection.execute("INSERT INTO calendar_check_dates SELECT * FROM calendar_check_dates LIMIT 1")


async def test_coverage_and_freshness(repositories: PersistenceRepositories) -> None:
    """T18: a check covering 10-27..10-30 does not verify 10-31 or later; a
    truncated response leaves the rest unverified; two brokers need two
    agreements; a 4-session-old agreement is not fresh."""
    alerts = TelegramAlerter(None, None)
    at = datetime(2026, 10, 27, 14, tzinfo=UTC)
    await run_check(repositories, _Broker(_library(date(2026, 10, 27), date(2026, 10, 30))), "alpaca", Venue.US_EQUITY, CAL, alerts, at)
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    assert await gate.status(Venue.US_EQUITY, date(2026, 10, 30), at) == "verified"
    assert await gate.status(Venue.US_EQUITY, date(2026, 10, 31), at) == "unverified"
    assert await gate.status(Venue.US_EQUITY, date(2026, 11, 2), at) == "unverified"
    two = CalendarGate(repositories, CAL, ("alpaca", "ibkr"))
    assert await two.status(Venue.US_EQUITY, date(2026, 10, 28), at) == "unverified"
    await run_check(repositories, _Broker(_library(date(2026, 10, 27), date(2026, 10, 30))), "ibkr", Venue.US_EQUITY, CAL, alerts, at)
    assert await two.status(Venue.US_EQUITY, date(2026, 10, 28), at) == "verified"
    # checked Tuesday 10-27; Fri 10-30 is 3 sessions later (fresh), Mon 11-02 is 4 (stale)
    assert gate.fresh(at, datetime(2026, 10, 30, 15, tzinfo=UTC))
    assert not gate.fresh(at, datetime(2026, 11, 2, 15, tzinfo=UTC))
    assert await gate.status(Venue.US_EQUITY, date(2026, 10, 30), datetime(2026, 11, 2, 15, tzinfo=UTC)) == "unverified"
    late = datetime(2026, 10, 28, 3, 59, tzinfo=UTC)  # 23:59 NY on the 27th counts for the 27th
    assert gate.fresh(late, datetime(2026, 10, 30, 15, tzinfo=UTC))


async def test_lifecycle_crashes_and_terminal_events(repositories: PersistenceRepositories) -> None:
    """T19."""
    database = repositories.audit_events.database
    alerts = TelegramAlerter(None, None)

    class Crash(Exception):
        pass

    class CrashAfterFetch:
        async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
            return _library(start, end)

    from tradepulse.calendar import checks

    real_compare = checks.compare

    def crash_compare(*args: Any) -> Any:
        raise Crash

    checks.compare = crash_compare  # type: ignore[assignment]
    try:
        with pytest.raises(Crash):
            await run_check(repositories, CrashAfterFetch(), "alpaca", Venue.US_EQUITY, CAL, alerts, NOW)
    finally:
        checks.compare = real_compare  # type: ignore[assignment]
    with sqlite3.connect(database.path) as connection:
        assert [r[0] for r in connection.execute("SELECT event FROM calendar_check_events ORDER BY created_at, event DESC")] == [
            "started", "fetched"]
        assert connection.execute("SELECT COUNT(*) FROM calendar_check_dates").fetchone()[0] == 0
    assert await CalendarGate(repositories, CAL, ("alpaca",)).status(Venue.US_EQUITY, date(2026, 11, 24), NOW) == "unverified"
    assert await close_interrupted(repositories, NOW + timedelta(minutes=9)) == []
    [closed] = await close_interrupted(repositories, NOW + timedelta(minutes=11))
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT json_extract(payload,'$.reason') FROM calendar_check_events WHERE event='failed'").fetchone() == (
            "interrupted",)
        with pytest.raises(sqlite3.IntegrityError, match="already ended"):  # a second terminal event
            connection.execute("INSERT INTO calendar_check_events (record_id,check_id,event,broker,venue,payload,created_at) "
                               "VALUES (?,?,?,?,?,?,?)", ("x", closed, "completed", "alpaca", "us_equity", "{}", NOW.isoformat()))
        with pytest.raises(sqlite3.IntegrityError, match="need a completed check"):  # date rows without completed
            connection.execute("INSERT INTO calendar_check_dates VALUES ('r', ?, 'alpaca', 'us_equity', '2026-11-24', 'agreed', ?, '{}', ?)",
                               (closed, NOW.isoformat(), NOW.isoformat()))
    failed = await run_check(repositories, _Broker(RuntimeError("timeout")), "alpaca", Venue.US_EQUITY, CAL, alerts, NOW)
    assert failed.outcome == "failed"
    assert await CalendarGate(repositories, CAL, ("alpaca",)).status(Venue.US_EQUITY, date(2026, 11, 24), NOW) == "unverified"


async def test_daily_checks_are_idempotent_and_alert_when_unverified(repositories: PersistenceRepositories) -> None:
    alerts = TelegramAlerter(None, None)
    assert len(await run_daily_checks(repositories, _sim(), "alpaca", CAL, alerts, NOW)) == 2
    assert await run_daily_checks(repositories, _sim(), "alpaca", CAL, alerts, NOW + timedelta(hours=2)) == []
    assert await _alerts(repositories, "calendar_unverified") == []
    day = NOW + timedelta(days=7)
    await run_daily_checks(repositories, _Broker(RuntimeError("down")), "alpaca", CAL, alerts, day)
    await run_daily_checks(repositories, _Broker(RuntimeError("down")), "alpaca", CAL, alerts, day + timedelta(hours=1))
    assert len(await _alerts(repositories, "calendar_unverified")) == 1  # once per day
