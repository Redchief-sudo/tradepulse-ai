"""Rev.144 task 5: the frozen calendar fingerprint and the Rev.143 deadline hook (§3.2, §3.8; T12, T16)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from test_cli import _settings

from tradepulse.alerts import TelegramAlerter
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue, ny_time
from tradepulse.calendar.checks import CalendarGate, run_check, session_deadline
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.verification.service import context_for, context_matches
from tradepulse.verification.soak import comparable_context

CAL = MarketCalendar.from_library()


def test_fingerprint_frozen_for_the_opening_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T12: the same library verifies on any later day; changed session data
    is frozen_configuration_mismatch; a pre-Rev.144 context still matches."""
    settings = _settings(f"sqlite:///{tmp_path}/v.db")
    frozen = context_for(settings, calendar_anchor=date(2026, 10, 29))
    assert frozen["market_calendar_fingerprint"]["library_version"] == "4.13.2"
    assert context_matches(frozen, settings)  # recomputed for the frozen anchor, today
    a0 = {key: value for key, value in frozen.items() if key != "market_calendar_fingerprint"}
    assert context_matches(a0, settings)

    changed = dict(CAL._equity)
    changed[date(2026, 12, 24)] = (ny_time(date(2026, 12, 24), time(9, 30)), ny_time(date(2026, 12, 24), time(16)))
    patched = MarketCalendar(changed, first=CAL.first, last=CAL.last)
    monkeypatch.setattr("tradepulse.calendar.market_calendar", lambda today=None: patched)
    assert not context_matches(frozen, settings)
    assert context_matches(a0, settings)  # A0 is unaffected


def test_soak_contexts_compare_by_library_version(tmp_path: Path) -> None:
    settings = _settings(f"sqlite:///{tmp_path}/v.db")
    one = comparable_context(context_for(settings, calendar_anchor=date(2026, 10, 29)))
    two = comparable_context(context_for(settings, calendar_anchor=date(2026, 11, 3)))
    assert one == two and one["market_calendar_fingerprint"] == "4.13.2"


class _Broker:
    def __init__(self, sessions: list[BrokerSession]) -> None:
        self.sessions = sessions

    async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
        return [s for s in self.sessions if start <= s.date <= end]


async def test_rev143_deadline_hook(tmp_path: Path) -> None:
    """T16: three sessions after Wednesday 11-25 is Tuesday 12-01's close;
    an unverified span gives the earlier of that and three weekdays."""
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/d.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    observed = datetime(2026, 11, 25, 15, tzinfo=UTC)
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    unverified, source = await session_deadline(gate, observed)
    assert (unverified, source) == (ny_time(date(2026, 11, 30), time(16)), "weekday_bound")  # Thu, Fri, Mon
    sessions = [BrokerSession(Venue.US_EQUITY, s.date, s.open, s.close, "t")
                for s in CAL.sessions_between(Venue.US_EQUITY, date(2026, 11, 25), date(2026, 12, 20))]
    await run_check(repositories, _Broker(sessions), "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None), observed)
    deadline, source = await session_deadline(gate, observed)
    assert (deadline, source) == (ny_time(date(2026, 12, 1), time(16)), "market_calendar")
    beyond = datetime.combine(CAL.last - timedelta(days=1), time(15), UTC)
    assert (await session_deadline(gate, beyond))[1] == "weekday_bound"
