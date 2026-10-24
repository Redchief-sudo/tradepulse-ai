#!/usr/bin/env python3
"""Check a Phase A0 verification run's window before it starts.

Exits 0 only if both the start and the end fall outside every XNYS session
and outside the 15 minutes before an open: a run that starts or ends while
prices move can flip a valuation snapshot to FAILED_INTEGRITY (the Rev.127
soak ended at the open and did exactly that). Holidays, weekends, early
closes and daylight-saving changes come from the exchange calendar. Crypto
trades continuously; the rule concerns equity and option sessions.

    scripts/a0_verification_window.py --start 2026-10-26T09:00-04:00 --hours 31.5
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tradepulse.broker.simulated import SimSession

NEW_YORK = ZoneInfo("America/New_York")
PRE_OPEN = timedelta(minutes=15)


def boundary_problem(session: SimSession, at: datetime) -> str | None:
    """Why ``at`` cannot be a run boundary, or None if it can."""
    if session.is_open(at):
        return "inside a trading session"
    upcoming = session.next_open(at)
    if upcoming is None:
        return "beyond the exchange calendar"
    if upcoming - at < PRE_OPEN:
        return f"within {int(PRE_OPEN.total_seconds() // 60)} minutes before the open at {upcoming.astimezone(NEW_YORK):%H:%M %Z}"
    return None


def check(start: datetime, hours: float) -> tuple[datetime, list[str]]:
    if start.tzinfo is None:
        raise ValueError("--start needs a UTC offset, e.g. 2026-10-26T09:00-04:00")
    end = start + timedelta(hours=hours)
    session = SimSession.from_exchange_calendar("XNYS", (start - timedelta(days=7)).date(),
                                                (end + timedelta(days=14)).date())
    problems = [f"{name} {at.isoformat()} is {problem}"
                for name, at in (("start", start), ("end", end))
                if (problem := boundary_problem(session, at)) is not None]
    return end, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat, help="ISO time with a UTC offset")
    parser.add_argument("--hours", required=True, type=float)
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    try:
        end, problems = check(args.start, args.hours)
    except ValueError as exc:
        parser.error(str(exc))
    for name, at in (("start", args.start), ("end", end)):
        print(f"{name}: {at.astimezone(ZoneInfo('UTC')):%Y-%m-%d %H:%M UTC}  {at.astimezone(NEW_YORK):%Y-%m-%d %H:%M %Z}")
    for problem in problems:
        print(f"REFUSED: {problem}", file=sys.stderr)
    if not problems:
        print("window ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
