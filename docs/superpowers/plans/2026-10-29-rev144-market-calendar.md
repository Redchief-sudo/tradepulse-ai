# Rev.144 Market Calendar Implementation Plan

> **For agentic workers:** executed natively (operator decision 2026-10-28: build all, one review). Steps use checkbox (`- [ ]`) syntax.

**Goal:** a broker-independent venue calendar from the pinned `exchange_calendars`. Each day it is cross-checked against every connected broker, and a new stock or option entry is authorized only when the calendar and the broker clock agree.

**Architecture:**
- **`tradepulse/calendar/`:** the pure library calendar (`MarketCalendar`).
- **Append-only check evidence:** `calendar_check_events`, `calendar_check_dates` and `instrument_hours`.
- **`CalendarGate`:** loads that evidence and answers `status` and `entry_window_open`.
- **The broker port** gains `get_calendar`.
- **Wiring:** the gateway calls the gate at its existing clock check; session sync feeds an in-memory clock/calendar disagreement monitor; reconciliation runs the daily check.

**Tech Stack:** Python 3.12, `exchange_calendars==4.13.2` (now a runtime dependency), SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-market-calendar-design.md` (rev. 3), plus the G0 results recorded in its §3.9.

## Global Constraints

- `exchange_calendars==4.13.2`, exactly; the library's default window (20 years back, 1 year ahead), never extrapolated.
- Freshness is 3 NYSE sessions, the disagreement grace is 5 minutes, interrupted checks are closed after 10 minutes, and a check covers [today, today + 30 days].
- Protective exits, crypto, the time stop, the stall exit and the risk day are untouched (C-3, C-5).
- Existing rejection strings are kept: `MARKET_CLOCK_UNAVAILABLE: …`, `EQUITY_MARKET_CLOSED`, `OPTION_MARKET_CLOSED`.

## Review Focus

1. **A gateway in a test or runtime with no completed check:** every entry is refused `CALENDAR_UNVERIFIED`. The runtime must therefore check at startup, before the first entry. A test in Task 4 pins this.
2. **The NY date boundary:** a check at 23:59 NY counts for its own date; freshness counts sessions after that date. A Task 3 test pins it.
3. **DST weeks:** Alpaca's NY-local "09:30" converts to 13:30 or 14:30 UTC correctly. A Task 2 test pins both March and November.
4. **A crash between `fetched` and `completed`:** that check confers nothing, and startup closes it. Task 3, T19.
5. **A manifest written before Rev.144** (A0's) still passes the frozen-configuration check. Task 5, T12.

---

### Task 1: The library calendar and packaging (§3.1, §3.2; T1–T3, T15)
- Create `tradepulse/calendar/__init__.py` with `Venue`, `Session`, `CalendarHorizonExceeded`, `MarketCalendar`, `venue_for(asset)` and `market_calendar(today)` (cached, rebuilt per NY date).
- Change `pyproject.toml` so `exchange_calendars==4.13.2` is a runtime dependency.
- Test: `python_tests/test_rev144_calendar.py`.
- Commit: `feat(calendar): pinned venue calendar from exchange_calendars (Rev.144 task 1)`.

### Task 2: The broker port's `get_calendar` (§3.3; T13)
- Add `BrokerSession` to `tradepulse/calendar/__init__.py`.
- Implement `get_calendar(start, end)` on `BrokerPort`, `AlpacaClient` (`/v2/calendar`, NY-local to UTC; one row per venue served: `us_equity` and `us_option`) and `SimulatedBroker` (from `SimSession`, with `inject_calendar_difference`).
- Add `tradepulse/calendar/ibkr_hours.py`, `parse_trading_hours(hours, time_zone_id, venue, source)`, for A2.
- Test: the G0 Alpaca fixture `python_tests/fixtures/rev144/alpaca_calendar.json`, the simulator, and an IBKR hours-string fixture.
- Commit: `feat(broker): get_calendar on the broker port (Rev.144 task 2)`.

### Task 3: Check evidence and status (§3.4, §3.4a, §3.7; T7, T8, T14, T17–T19)
- Schema: three append-only tables with Rev.140's triggers, plus a `BEFORE INSERT` trigger that refuses a second terminal event and date rows without `completed`.
- `tradepulse/calendar/checks.py`:
  - `run_check(repositories, broker, broker_name, venue, calendar, alerts, now)`;
  - `run_daily_checks(...)`, idempotent per NY date;
  - `close_interrupted(repositories, now)`;
  - `CalendarGate.load(repositories, calendar, brokers, now)` with `status(venue, day)` and `instrument_hours_valid(...)`.
- Commit: `feat(calendar): immutable cross-check lifecycle and per-date status (Rev.144 task 3)`.

### Task 4: Entry authorization (§3.5, §3.6; T4–T6, T9–T11)
- `CalendarGate.entry_window_open(broker, asset, t, broker_clock) -> EntryWindow`.
- `tradepulse/calendar/disagreement.py`: `ClockCalendarMonitor`, the process singleton fed by session sync.
- Gateway: one call replaces `if not market_clock.is_open`.
- Reconciliation runs `run_daily_checks`; startup closes interrupted checks and checks immediately.
- Existing gateway tests get a `calendar_verified` fixture.
- Commit: `feat(execution): entries need a verified calendar and an agreeing clock (Rev.144 task 4)`.

### Task 5: Fingerprint and the Rev.143 hook (§3.2, §3.8; T12, T16)
- `context_for(settings, calendar_anchor=None)` adds `market_calendar_fingerprint = {anchor, library_version, sha256}`.
- The check recomputes it for the manifest's anchor, and only if the manifest has the field.
- `comparable_context` compares the library version.
- Add `session_deadline(gate, first_observed_at, n) -> (deadline, source)`.
- Commit: `feat(verification): frozen calendar fingerprint; trading-day deadline hook (Rev.144 task 5)`.

### Task 6: Rev record
- Create `docs/rev144-market-calendar.md`, then run the full suite, mypy and Ruff.
- Commit: `docs: Rev.144 record (Rev.144 task 6)`.
