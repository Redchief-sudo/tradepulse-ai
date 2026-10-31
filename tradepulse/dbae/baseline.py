"""The calendar-bound session baseline (spec §4.2, §4.3, §11, §13.1).

The baseline for session date D is the account's closing equity on the
trading date before D, read from the daily portfolio-history bar and
cross-checked against ``last_equity``. It is the only evidence that can
raise an account's band, so a deposit, an intraday gain or a restart never
unlocks a higher band before the next session. It is verified or absent:
a failed check writes no row, refuses entries for that decision and is
retried at the next one. An older baseline never stands in for a later
session.

Persistence and the calendar gate are imported for annotations only: the
hydration registry imports this module for its decoder.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import httpx

from tradepulse.broker.capabilities import DailyEquityBar, PortfolioHistory
from tradepulse.broker.errors import AlpacaDataIntegrityError, AlpacaError
from tradepulse.calendar import CalendarHorizonExceeded, MarketCalendar, Venue, ny_date
from tradepulse.models import AuditEvent
from tradepulse.time import aware_utc

if TYPE_CHECKING:
    from tradepulse.calendar.checks import CalendarGate
    from tradepulse.persistence import PersistenceRepositories

BASELINE_REASON: Literal["DBAE_SESSION_BASELINE_UNVERIFIED"] = "DBAE_SESSION_BASELINE_UNVERIFIED"
AGREEMENT_TOLERANCE = Decimal("0.01")
SESSION_SEARCH_DAYS = 14
_SOURCE: Literal["portfolio_history"] = "portfolio_history"


@dataclass(frozen=True, slots=True)
class SessionBaseline:
    session_date: date
    previous_trading_date: date  # the date of the bar used
    previous_close_equity: Decimal
    last_equity: Decimal  # at capture
    source: Literal["portfolio_history"]
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class BaselineRefusal:
    reason: Literal["DBAE_SESSION_BASELINE_UNVERIFIED"]
    check: str  # the failing check, e.g. "previous_bar_missing" or "calendar_unverified"


def decode_session_baseline(payload: Mapping[str, Any]) -> SessionBaseline:
    """The ``dbae_session_baselines`` hydrator (registered in persistence.hydration)."""
    if payload["source"] != _SOURCE:
        raise ValueError(f"unknown session baseline source: {payload['source']!r}")
    return SessionBaseline(
        session_date=date.fromisoformat(payload["session_date"]),
        previous_trading_date=date.fromisoformat(payload["previous_trading_date"]),
        previous_close_equity=Decimal(str(payload["previous_close_equity"])),
        last_equity=Decimal(str(payload["last_equity"])),
        source=_SOURCE,
        captured_at=aware_utc(payload["captured_at"]),
    )


def _latest_session_on_or_before(calendar: MarketCalendar, day: date) -> date:
    sessions = calendar.sessions_between(Venue.US_EQUITY, day - timedelta(days=SESSION_SEARCH_DAYS), day)
    if not sessions:
        raise ValueError(f"NO_RECENT_SESSION: no {Venue.US_EQUITY.value} session in the {SESSION_SEARCH_DAYS} days "
                         f"to {day.isoformat()}")
    return sessions[-1].date


def session_date_for(calendar: MarketCalendar, now: datetime) -> date:
    """The New York date if it is a US equity session, otherwise the most
    recent session before it. Raises CalendarHorizonExceeded outside the
    calendar window and ValueError NO_RECENT_SESSION if none is found."""
    return _latest_session_on_or_before(calendar, ny_date(now))


def previous_trading_date(calendar: MarketCalendar, session_date: date) -> date:
    """The latest US equity session strictly before ``session_date``."""
    return _latest_session_on_or_before(calendar, session_date - timedelta(days=1))


def band_equity(current_equity: Decimal | None, baseline: SessionBaseline | BaselineRefusal) -> Decimal | None:
    """§4.2: the lower of the fresh equity and the previous close. None
    (unknown) without a verified baseline or a current equity."""
    if isinstance(baseline, BaselineRefusal) or current_equity is None:
        return None
    return min(current_equity, baseline.previous_close_equity)


async def _refuse(repositories: PersistenceRepositories, session_date: date, check: str, now: datetime,
                  details: Mapping[str, Any]) -> BaselineRefusal:
    """No baseline row; one ``dbae_session_baseline_unverified`` per date and check."""
    event_id = f"dbae_baseline_unverified:{session_date.isoformat()}:{check}"
    await repositories.audit_events.create_once(event_id, AuditEvent(
        event_id=event_id, event_type="dbae_session_baseline_unverified", severity="warning",
        message=f"DBAE_SESSION_BASELINE_UNVERIFIED: {check} for session {session_date.isoformat()}; "
                "no new entries until a baseline is verified.",
        occurred_at=now, entity_type="dbae_session_baseline", entity_id=session_date.isoformat(),
        details={"session_date": session_date.isoformat(), "check": check, **details}))
    return BaselineRefusal(reason=BASELINE_REASON, check=check)


async def _read_bars(history: PortfolioHistory) -> list[DailyEquityBar] | None:
    """None when the read fails: timeout, HTTP failure or malformed data (§10)."""
    try:
        return await history.get_daily_equity_bars()
    except (AlpacaError, AlpacaDataIntegrityError, httpx.HTTPError, ValueError):
        return None


async def load_or_capture_baseline(repositories: PersistenceRepositories, gate: CalendarGate,
                                   history: PortfolioHistory, last_equity: Decimal | None,
                                   now: datetime) -> SessionBaseline | BaselineRefusal:
    """The verified baseline for the current session, captured on first use
    and reused afterwards (including after a restart), or the refusal naming
    the first check that failed (§4.3)."""
    calendar = gate.calendar
    try:
        session_date = session_date_for(calendar, now)
        previous_date = previous_trading_date(calendar, session_date)
    except CalendarHorizonExceeded:
        return await _refuse(repositories, ny_date(now), "calendar_beyond_horizon", now, {})
    except ValueError:
        return await _refuse(repositories, ny_date(now), "calendar_no_recent_session", now, {})
    key = session_date.isoformat()

    stored = await repositories.dbae_session_baselines.get(key)
    if stored is not None:
        return decode_session_baseline(stored["payload"])

    status = await gate.status(Venue.US_EQUITY, session_date, now)
    if status != "verified":
        return await _refuse(repositories, session_date, f"calendar_{status}", now, {})

    if last_equity is None or not last_equity.is_finite():
        return await _refuse(repositories, session_date, "last_equity_unknown", now,
                             {"previous_trading_date": previous_date.isoformat()})

    bars = await _read_bars(history)
    if bars is None:
        return await _refuse(repositories, session_date, "history_unavailable", now,
                             {"previous_trading_date": previous_date.isoformat()})
    # Only the bar for exactly the previous trading date; a bar for the
    # current session, if one appears, is never used (§4.3, §13.1).
    matching = [bar for bar in bars if bar.trading_date == previous_date]
    evidence: dict[str, Any] = {"previous_trading_date": previous_date.isoformat(), "last_equity": last_equity,
                                "bars": len(bars)}
    if not matching:
        return await _refuse(repositories, session_date, "previous_bar_missing", now, evidence)
    if len(matching) > 1:
        return await _refuse(repositories, session_date, "previous_bar_ambiguous", now, evidence)
    close = matching[0].equity
    if not close.is_finite() or close < 0:
        return await _refuse(repositories, session_date, "previous_bar_invalid", now, evidence)
    difference = abs(close - last_equity)
    evidence |= {"previous_close_equity": close, "difference": difference}
    if difference > AGREEMENT_TOLERANCE:
        return await _refuse(repositories, session_date, "last_equity_disagrees", now, evidence)

    baseline = SessionBaseline(session_date=session_date, previous_trading_date=previous_date,
                               previous_close_equity=close, last_equity=last_equity, source=_SOURCE, captured_at=now)
    if not await repositories.dbae_session_baselines.create_once(key, baseline):
        # Another caller captured this session first: its row is the baseline.
        winner = await repositories.dbae_session_baselines.get(key)
        if winner is None:
            raise RuntimeError(f"dbae_session_baselines: {key} neither inserted nor present")
        return decode_session_baseline(winner["payload"])
    event_id = f"dbae_baseline_captured:{key}"
    await repositories.audit_events.create_once(event_id, AuditEvent(
        event_id=event_id, event_type="dbae_session_baseline_captured", severity="info",
        message=f"DBAE session baseline for {key}: previous close {close} on {previous_date.isoformat()}.",
        occurred_at=now, entity_type="dbae_session_baseline", entity_id=key,
        details={"session_date": key, "previous_close_equity": close, "previous_trading_date": previous_date.isoformat(),
                 "last_equity": last_equity, "difference": difference, "agreement": "agreed",
                 "source": _SOURCE}))
    return baseline
