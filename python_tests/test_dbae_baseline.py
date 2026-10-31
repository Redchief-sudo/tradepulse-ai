"""DBAE task 6: the calendar-bound session baseline (spec §4.2, §4.3, §11, §13.1)."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.capabilities import DailyEquityBar
from tradepulse.broker.errors import AlpacaDataIntegrityError, AlpacaError
from tradepulse.broker.simulated import SimulatedCapabilities
from tradepulse.calendar import BrokerSession, MarketCalendar, Venue, ny_date
from tradepulse.calendar.checks import CalendarGate, run_check
from tradepulse.dbae import Band, band_for
from tradepulse.dbae.baseline import (
    BaselineRefusal,
    SessionBaseline,
    band_equity,
    load_or_capture_baseline,
    session_date_for,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.database import DatabaseError

CAL = MarketCalendar.from_library()
THU, FRI, SAT = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7)
MON, TUE = date(2026, 11, 9), date(2026, 11, 10)


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    """An aware UTC instant at a New York wall-clock time (EST in November)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) + timedelta(hours=5)


class _Broker:
    async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
        return [BrokerSession(v, s.date, s.open, s.close, "test")
                for v in (Venue.US_EQUITY, Venue.US_OPTION) for s in CAL.sessions_between(v, start, end)]


@pytest.fixture
async def repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/baseline.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def verified_gate(repositories: PersistenceRepositories, checked_on: date) -> CalendarGate:
    """The Rev.144 library calendar, cross-checked by an agreeing broker at
    09:00 New York on ``checked_on`` (covering that date and the next 30)."""
    result = await run_check(repositories, _Broker(), "alpaca", Venue.US_EQUITY, CAL, TelegramAlerter(None, None),
                             ny(checked_on, 9))
    assert result.outcome == "completed"
    return CalendarGate(repositories, CAL, ("alpaca",))


def history(*bars: tuple[date, str]) -> SimulatedCapabilities:
    capabilities = SimulatedCapabilities()
    capabilities.bars = [DailyEquityBar(trading_date=day, equity=Decimal(equity)) for day, equity in bars]
    return capabilities


async def baseline_rows(repositories: PersistenceRepositories) -> list[Any]:
    return list(await paginate_all_rows(repositories.dbae_session_baselines))


async def events(repositories: PersistenceRepositories, kind: str) -> list[Any]:
    return [row for row in await paginate_all_rows(repositories.audit_events) if row["payload"]["event_type"] == kind]


# --- which session -------------------------------------------------------------


def test_session_date_for_session_weekend_and_holiday() -> None:
    assert session_date_for(CAL, ny(TUE, 3)) == TUE  # pre-market is still the session's date
    assert session_date_for(CAL, ny(SAT, 12)) == FRI
    assert session_date_for(CAL, ny(date(2026, 11, 8), 23)) == FRI  # Sunday
    assert session_date_for(CAL, ny(date(2026, 12, 25), 12)) == date(2026, 12, 24)  # Christmas


def test_session_date_for_no_recent_session_raises() -> None:
    sessions = {d: CAL.session(Venue.US_EQUITY, d) for d in (date(2026, 1, 2),)}
    sparse = MarketCalendar({d: (s.open, s.close) for d, s in sessions.items() if s is not None},
                            first=date(2025, 1, 1), last=date(2027, 1, 1))
    with pytest.raises(ValueError, match="NO_RECENT_SESSION"):
        session_date_for(sparse, ny(date(2026, 6, 1), 12))


# --- band equity: downgrades at once, upgrades only through a new baseline -----


async def test_deposit_then_noon_start_stays_micro_plus(repositories: PersistenceRepositories) -> None:
    """A morning deposit lifts equity to $550; starting at noon still yields
    yesterday's $50 close, so the band stays MICRO_PLUS."""
    gate = await verified_gate(repositories, TUE)
    result = await load_or_capture_baseline(repositories, gate, history((MON, "50")), Decimal(50), ny(TUE, 12))
    assert isinstance(result, SessionBaseline)
    assert result.previous_close_equity == Decimal(50) and result.previous_trading_date == MON
    assert band_for(band_equity(Decimal(550), result)) == Band.MICRO_PLUS


async def test_intraday_gain_no_upgrade(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    baseline = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert isinstance(baseline, SessionBaseline)
    assert band_equity(Decimal(140), baseline) == Decimal(60)
    assert band_for(band_equity(Decimal(140), baseline)) == Band.MICRO_PLUS


async def test_restart_reuses_stored_baseline(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    first = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    # After a restart, history and last_equity now differ; the stored row stands.
    again = await load_or_capture_baseline(repositories, gate, history((MON, "900")), Decimal(900), ny(TUE, 14))
    assert isinstance(first, SessionBaseline) and again == first
    assert len(await baseline_rows(repositories)) == 1
    assert len(await events(repositories, "dbae_session_baseline_captured")) == 1


async def test_intraday_loss_downgrades_at_once(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    baseline = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert band_for(band_equity(Decimal(45), baseline)) == Band.MICRO


def test_band_equity_unknown_is_none() -> None:
    refusal = BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="previous_bar_missing")
    baseline = SessionBaseline(session_date=TUE, previous_trading_date=MON, previous_close_equity=Decimal(60),
                               last_equity=Decimal(60), source="portfolio_history", captured_at=ny(TUE, 10))
    assert band_equity(Decimal(100), refusal) is None
    assert band_equity(None, baseline) is None


# --- verification fails closed -------------------------------------------------


async def test_last_equity_disagreement_refuses_even_with_older_baseline(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, MON)
    older = await load_or_capture_baseline(repositories, gate, history((FRI, "80")), Decimal(80), ny(MON, 10))
    assert isinstance(older, SessionBaseline) and older.session_date == MON
    result = await load_or_capture_baseline(repositories, gate, history((FRI, "80"), (MON, "70")), Decimal(80),
                                            ny(TUE, 8))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="last_equity_disagrees")
    assert band_equity(Decimal(70), result) is None
    assert [row["record_id"] for row in await baseline_rows(repositories)] == [MON.isoformat()]
    unverified = await events(repositories, "dbae_session_baseline_unverified")
    assert [row["record_id"] for row in unverified] == [f"dbae_baseline_unverified:{TUE.isoformat()}:last_equity_disagrees"]


async def test_agreement_tolerance_is_one_cent(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    bars = history((MON, "100.00"))
    refused = await load_or_capture_baseline(repositories, gate, bars, Decimal("100.02"), ny(TUE, 9))
    assert isinstance(refused, BaselineRefusal) and refused.check == "last_equity_disagrees"
    captured = await load_or_capture_baseline(repositories, gate, bars, Decimal("100.01"), ny(TUE, 9))
    assert isinstance(captured, SessionBaseline) and captured.last_equity == Decimal("100.01")


async def test_premarket_retry_succeeds_once_agreeing(repositories: PersistenceRepositories) -> None:
    """Pre-market, last_equity has not rolled to Monday's close: refused, no
    row; the next decision, once it agrees, captures the baseline."""
    gate = await verified_gate(repositories, TUE)
    bars = history((FRI, "80"), (MON, "95"))
    early = await load_or_capture_baseline(repositories, gate, bars, Decimal(80), ny(TUE, 6))
    assert early == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="last_equity_disagrees")
    again = await load_or_capture_baseline(repositories, gate, bars, Decimal(80), ny(TUE, 6, 30))
    assert isinstance(again, BaselineRefusal)
    assert await baseline_rows(repositories) == []
    assert len(await events(repositories, "dbae_session_baseline_unverified")) == 1  # once per date and check
    later = await load_or_capture_baseline(repositories, gate, bars, Decimal(95), ny(TUE, 9, 15))
    assert isinstance(later, SessionBaseline)
    assert (later.previous_close_equity, later.previous_trading_date, later.captured_at) == (Decimal(95), MON, ny(TUE, 9, 15))
    captured = await events(repositories, "dbae_session_baseline_captured")
    assert [row["record_id"] for row in captured] == [f"dbae_baseline_captured:{TUE.isoformat()}"]
    details = captured[0]["payload"]["details"]
    assert details["session_date"] == TUE.isoformat() and details["previous_trading_date"] == MON.isoformat()
    assert Decimal(details["previous_close_equity"]) == Decimal(95) and Decimal(details["last_equity"]) == Decimal(95)
    assert details["agreement"] == "agreed"


async def test_missing_previous_bar_refuses(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    result = await load_or_capture_baseline(repositories, gate, history((FRI, "80")), Decimal(80), ny(TUE, 10))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="previous_bar_missing")
    assert await baseline_rows(repositories) == []


async def test_new_account_refused_until_next_session(repositories: PersistenceRepositories) -> None:
    """Funded Monday: no bar before Monday, so no entries on Monday; Tuesday
    has Monday's closing bar."""
    gate = await verified_gate(repositories, MON)
    monday = await load_or_capture_baseline(repositories, gate, history(), Decimal(0), ny(MON, 11))
    assert isinstance(monday, BaselineRefusal) and monday.check == "previous_bar_missing"
    tuesday = await load_or_capture_baseline(repositories, gate, history((MON, "40")), Decimal(40), ny(TUE, 10))
    assert isinstance(tuesday, SessionBaseline) and band_for(band_equity(Decimal(40), tuesday)) == Band.MICRO


async def test_last_equity_unknown_refuses(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    result = await load_or_capture_baseline(repositories, gate, history((MON, "60")), None, ny(TUE, 10))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="last_equity_unknown")
    nan = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal("NaN"), ny(TUE, 10))
    assert nan == result


@pytest.mark.parametrize("failure", [
    AlpacaError("unavailable", 503, None, None, "getPortfolioHistory"),
    AlpacaDataIntegrityError("getPortfolioHistory: timestamp and equity must be lists of equal length"),
])
async def test_history_failure_refuses(repositories: PersistenceRepositories, failure: Exception) -> None:
    class Failing:
        async def get_daily_equity_bars(self) -> list[DailyEquityBar]:
            raise failure

    gate = await verified_gate(repositories, TUE)
    result = await load_or_capture_baseline(repositories, gate, Failing(), Decimal(60), ny(TUE, 10))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="history_unavailable")


async def test_invalid_previous_bar_refuses(repositories: PersistenceRepositories) -> None:
    """A negative or non-finite bar (should the source ever pass one) never
    becomes a baseline; two bars for the date are ambiguous."""
    gate = await verified_gate(repositories, TUE)
    negative = await load_or_capture_baseline(repositories, gate, history((MON, "-1")), Decimal(-1), ny(TUE, 10))
    assert isinstance(negative, BaselineRefusal) and negative.check == "previous_bar_invalid"
    twice = await load_or_capture_baseline(repositories, gate, history((MON, "60"), (MON, "61")), Decimal(60),
                                           ny(TUE, 10))
    assert isinstance(twice, BaselineRefusal) and twice.check == "previous_bar_ambiguous"
    assert await baseline_rows(repositories) == []


async def test_unverified_calendar_refuses(repositories: PersistenceRepositories) -> None:
    gate = CalendarGate(repositories, CAL, ("alpaca",))  # no completed cross-check
    result = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="calendar_unverified")
    assert await baseline_rows(repositories) == []
    assert [row["record_id"] for row in await events(repositories, "dbae_session_baseline_unverified")] == [
        f"dbae_baseline_unverified:{TUE.isoformat()}:calendar_unverified"]


# --- bar dates (G1, §13.1) ------------------------------------------------------


async def test_monday_uses_friday_bar(repositories: PersistenceRepositories) -> None:
    """The bar stamped Saturday 00:00 UTC maps to Friday, the trading date before Monday."""
    friday = ny_date(datetime(2026, 11, 7, 0, 0, tzinfo=UTC))
    assert friday == FRI
    gate = await verified_gate(repositories, MON)
    result = await load_or_capture_baseline(repositories, gate, history((THU, "70"), (friday, "75")), Decimal(75),
                                            ny(MON, 10))
    assert isinstance(result, SessionBaseline)
    assert (result.session_date, result.previous_trading_date, result.previous_close_equity) == (MON, FRI, Decimal(75))


async def test_first_session_after_holiday(repositories: PersistenceRepositories) -> None:
    """Monday 2026-12-28 follows Christmas (Friday): the previous trading date is Thursday 12-24."""
    monday, thursday = date(2026, 12, 28), date(2026, 12, 24)
    gate = await verified_gate(repositories, monday)
    result = await load_or_capture_baseline(repositories, gate, history((date(2026, 12, 23), "300"), (thursday, "310")),
                                            Decimal(310), ny(monday, 10))
    assert isinstance(result, SessionBaseline)
    assert (result.previous_trading_date, result.previous_close_equity) == (thursday, Decimal(310))


async def test_saturday_crypto_uses_friday_session(repositories: PersistenceRepositories) -> None:
    """Saturday's session date is Friday, so its baseline is Friday's (Thursday's
    close); Friday's own bar is the session's bar and is never used."""
    gate = await verified_gate(repositories, THU)  # fresh for Friday on Saturday
    result = await load_or_capture_baseline(repositories, gate, history((THU, "120"), (FRI, "130")), Decimal(120),
                                            ny(SAT, 11))
    assert isinstance(result, SessionBaseline)
    assert (result.session_date, result.previous_trading_date, result.previous_close_equity) == (FRI, THU, Decimal(120))
    assert [row["record_id"] for row in await baseline_rows(repositories)] == [FRI.isoformat()]


async def test_saturday_refuses_when_friday_unverified(repositories: PersistenceRepositories) -> None:
    gate = CalendarGate(repositories, CAL, ("alpaca",))
    result = await load_or_capture_baseline(repositories, gate, history((THU, "120")), Decimal(120), ny(SAT, 11))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="calendar_unverified")


async def test_current_session_bar_ignored(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    result = await load_or_capture_baseline(repositories, gate, history((MON, "60"), (TUE, "500")), Decimal(60),
                                            ny(TUE, 15))
    assert isinstance(result, SessionBaseline) and result.previous_close_equity == Decimal(60)
    wednesday = date(2026, 11, 11)
    only_current = await load_or_capture_baseline(repositories, gate, history((wednesday, "500")), Decimal(500),
                                                  ny(wednesday, 15))
    assert isinstance(only_current, BaselineRefusal) and only_current.check == "previous_bar_missing"


# --- write-once storage ---------------------------------------------------------


async def test_concurrent_capture_writes_once(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    bars = history((MON, "250"))
    first, second = await asyncio.gather(
        load_or_capture_baseline(repositories, gate, bars, Decimal(250), ny(TUE, 10)),
        load_or_capture_baseline(repositories, gate, bars, Decimal(250), ny(TUE, 10)),
    )
    assert isinstance(first, SessionBaseline) and first == second
    assert len(await baseline_rows(repositories)) == 1
    assert len(await events(repositories, "dbae_session_baseline_captured")) == 1


async def test_stored_row_round_trips_and_is_append_only(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    baseline = await load_or_capture_baseline(repositories, gate, history((MON, "60.25")), Decimal("60.25"), ny(TUE, 10))
    row = await repositories.dbae_session_baselines.get(TUE.isoformat())
    assert row is not None and hydrate("dbae_session_baselines", row["payload"]) == baseline
    database = repositories.dbae_session_baselines.database
    for statement in ("UPDATE dbae_session_baselines SET payload='{}'", "DELETE FROM dbae_session_baselines"):
        with pytest.raises(DatabaseError, match="append-only"):
            await database.run(lambda c, sql=statement: c.execute(sql), write=True)


# --- fix round 1 ------------------------------------------------------------------


async def test_naive_now_raises_before_any_write(repositories: PersistenceRepositories) -> None:
    gate = await verified_gate(repositories, TUE)
    audit_before = len(await paginate_all_rows(repositories.audit_events))
    with pytest.raises(ValueError, match="now: mandatory aware timestamp invalid"):
        await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10).replace(tzinfo=None))
    assert await baseline_rows(repositories) == []
    assert len(await paginate_all_rows(repositories.audit_events)) == audit_before
    later = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert isinstance(later, SessionBaseline) and later.captured_at == ny(TUE, 10)


async def test_captured_event_repaired_after_failed_audit_write(repositories: PersistenceRepositories,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The row commits, the captured event write fails: the next call returns
    the stored row and writes the §11 event then, once."""
    gate = await verified_gate(repositories, TUE)
    real = type(repositories.audit_events).create_once

    async def failing(self: Any, record_id: str, payload: Any, **kwargs: Any) -> bool:
        if record_id.startswith("dbae_baseline_captured:"):
            raise DatabaseError("disk I/O error")
        return await real(self, record_id, payload, **kwargs)

    monkeypatch.setattr(type(repositories.audit_events), "create_once", failing)
    with pytest.raises(DatabaseError):
        await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert len(await baseline_rows(repositories)) == 1
    assert await events(repositories, "dbae_session_baseline_captured") == []
    monkeypatch.setattr(type(repositories.audit_events), "create_once", real)
    stored = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 11))
    again = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 12))
    assert isinstance(stored, SessionBaseline) and stored == again and stored.captured_at == ny(TUE, 10)
    captured = await events(repositories, "dbae_session_baseline_captured")
    assert [row["record_id"] for row in captured] == [f"dbae_baseline_captured:{TUE.isoformat()}"]
    details = captured[0]["payload"]["details"]
    assert Decimal(details["previous_close_equity"]) == Decimal(60) and details["previous_trading_date"] == MON.isoformat()


@pytest.mark.parametrize("payload", [
    {"session_date": "2026-11-10"},  # fields missing
    {"session_date": "2026-11-10", "previous_trading_date": "2026-11-09", "previous_close_equity": "abc",
     "last_equity": "60", "source": "portfolio_history", "captured_at": "2026-11-10T15:00:00+00:00"},
    {"session_date": "2026-11-10", "previous_trading_date": "2026-11-09", "previous_close_equity": "60",
     "last_equity": "60", "source": "portfolio_history", "captured_at": "2026-11-10T15:00:00"},  # naive
    {"session_date": "2026-11-09", "previous_trading_date": "2026-11-06", "previous_close_equity": "60",
     "last_equity": "60", "source": "portfolio_history", "captured_at": "2026-11-09T15:00:00+00:00"},  # wrong date
])
async def test_corrupt_stored_row_refuses(repositories: PersistenceRepositories, payload: dict[str, Any]) -> None:
    gate = await verified_gate(repositories, TUE)
    await repositories.dbae_session_baselines.create_once(TUE.isoformat(), payload)
    result = await load_or_capture_baseline(repositories, gate, history((MON, "60")), Decimal(60), ny(TUE, 10))
    assert result == BaselineRefusal(reason="DBAE_SESSION_BASELINE_UNVERIFIED", check="baseline_corrupt")
    assert len(await baseline_rows(repositories)) == 1  # no new row, the stored one untouched
    assert [row["record_id"] for row in await events(repositories, "dbae_session_baseline_unverified")] == [
        f"dbae_baseline_unverified:{TUE.isoformat()}:baseline_corrupt"]
    assert await events(repositories, "dbae_session_baseline_captured") == []
