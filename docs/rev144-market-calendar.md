# Rev.144 — Shared market calendar, cross-checked against every broker

**Spec:** `docs/superpowers/specs/2026-10-27-market-calendar-design.md` (rev. 3, plus the G0 results in §3.9)
**Plan:** `docs/superpowers/plans/2026-10-29-rev144-market-calendar.md`
**Branch:** `rev144`, stacked on Rev.141 (on Rev.140, on Rev.145). Six commits ending `(Rev.144 task N)`, plus the G0 and plan commit `d11c3c67`.

New stock and option entries now need two witnesses to agree: the broker's live clock and the pinned exchange calendar, verified that day by every connected broker. Protective exits, crypto, the time stop, the stall exit and the risk day are unchanged.

## G0 (read-only, 2026-10-29)

Evidence: `~/tradepulse-evidence/2026-10-29-rev144-g0/` (manifest SHA-256 prefix `1a081927646f223f`).

- **Alpaca `/v2/calendar`:**
  - `open` and `close` are New York local `HH:MM`; holidays are absent; the early closes (11-27, 12-24) are `13:00`.
  - Over 2026-10-27 to 2027-01-31 it gives 65 sessions with **no difference** from `exchange_calendars` 4.13.2 XNYS.
- **Alpaca has no per-contract option hours** (contracts, assets and clock endpoints probed), so late-close option entries on Alpaca stay refused.
- **IBKR was not measured:** TWS was not running. This item moves to A2's G0, and the `tradingHours` parser ships against a fixture in IBKR's documented format.

## What changed

| Spec | Change | Commit |
|---|---|---|
| §3.1, §3.2 | `tradepulse/calendar`: `Venue`, `Session`, `MarketCalendar` from XNYS within the library's default window. Past it raises `CalendarHorizonExceeded`; nothing is extrapolated. Options use a 16:00 close (13:00 on early closes); crypto is always open. The calendar is rebuilt once per NY date. `exchange_calendars==4.13.2` becomes a runtime dependency. | task 1 `80255853` |
| §3.3 | `BrokerPort.get_calendar` for Alpaca (`/v2/calendar`) and `SimulatedBroker` (with `inject_calendar_difference`), plus the IBKR hours parser | task 2 `ae908f86` |
| §3.4, §3.4a, §3.7 | Three append-only tables, with Rev.140's triggers and two insert triggers. The `started` → `fetched` → `completed`/`failed` lifecycle, closing of interrupted checks, the daily checks, `CalendarGate.status` and instrument-hours validity. | task 3 `e07fc045` |
| §3.5, §3.6 | `entry_window_open` at the gateway, the clock–calendar disagreement monitor in session sync, the daily check in reconciliation and at `run` startup | task 4 `1bc2d02f` |
| §3.2, §3.8 | `market_calendar_fingerprint` in the frozen verification context; `session_deadline` for Rev.143 | task 5 `74df298f` |

## Behaviour changes (spec §6)

- **New stock and option entries can be refused** for:
  - a calendar holiday or an early-close afternoon (`CALENDAR_SESSION_CLOSED`);
  - a contested date (`CALENDAR_DATE_CONTESTED`);
  - an unverified date (`CALENDAR_UNVERIFIED`);
  - a date past the library's horizon (`CALENDAR_HORIZON_EXCEEDED`);
  - a clock–calendar disagreement (`CALENDAR_CLOCK_DISAGREEMENT`);
  - an option after 16:00 without its own broker-verified hours (`OPTION_INSTRUMENT_HOURS_UNVERIFIED`).
- **`EQUITY_MARKET_CLOSED`, `OPTION_MARKET_CLOSED` and `MARKET_CLOCK_UNAVAILABLE`** keep their strings.
- **One more Alpaca read** per venue (equity, option) per NY date: `GET /v2/calendar`.
- **New critical alerts:** `CALENDAR_CONTESTED`, `CALENDAR_UNVERIFIED`, `CALENDAR_HORIZON_EXCEEDED` and `CALENDAR_CLOCK_DISAGREEMENT`.
- **Unchanged:** exits, crypto, the time stop, the stall exit and the risk day.

## Deviations and interpretations (for the reviewer)

1. **Coverage.** A check covers the span from the first to the last session in the broker's response.
   - Inside that span, a library session the broker omits, or a broker session the library lacks, is **contested**. This is how T7's "extra holiday" is detected; Alpaca lists only sessions.
   - A date past the span is not recorded, and stays **unverified** (T18).
2. **Interface.** `status` and `entry_window_open` take the database evidence, so they live on `CalendarGate` (`checks.py`), not on the pure `MarketCalendar`. Signature: `entry_window_open(gate, broker, asset, t, broker_clock, monitor=None)`.
3. **The gateway checks for itself.** When today has no completed check (a one-shot `scan`, or the first entry of a new date), the gateway runs the daily check before deciding. Without this, any command other than `run`/`reconcile` would refuse every entry.
4. **Disagreement.**
   - It is checked right after the date status, before the regular-session step.
   - It is measured against US-equity regular hours. Only session sync (the scan cycle) feeds it; the monitor's clock read does not.
   - It alerts through `alert_once`, once per episode start.
5. **Fingerprint.**
   - It is frozen with its anchor date (`{anchor, library_version, sha256}`), and recomputed for that anchor at each check, so it does not move daily.
   - Soak and run contexts open on different dates, so `comparable_context` compares them by library version.
6. **Late-close window.** `OPTION_INSTRUMENT_HOURS_UNVERIFIED` is reported up to 15 minutes after the venue close, the longest known option late close; after that the result is `OPTION_MARKET_CLOSED`.
7. **Broker calendar shapes.**
   - Alpaca's one calendar is reported for both US venues, equity and option.
   - An always-open simulator publishes no calendar.
8. **Test seams.**
   - `MarketCalendar(option_close_from_equity=True)` exists for test calendars only.
   - Four files written before Rev.144 opt in to an `open_market_calendar` fixture: `test_execution_gateway`, `test_scanner_coordinator`, `test_session_lifecycle` and `test_restart_survival`. They mock an open broker clock on the wall clock, and would otherwise depend on the hour they run. The fixture makes every NY day a session and has the mocked brokers report it.
   - The process disagreement monitor is reset around every test.
   - `test_broker_port` lists `get_calendar`; the conformance call-coverage test includes it.
9. **Approval evidence.** Spec §11 asks for Rev.140 and Rev.141 merged first. Under the operator's 2026-10-28 decision (build all, one review), Rev.144 is instead stacked on their branches, and everything merges after the one review.

## Tests

| Test | File |
|---|---|
| T1–T3, T15 | `test_rev144_calendar.py` |
| T13 | `test_rev144_broker_calendar.py`, using the recorded G0 Alpaca response in `fixtures/rev144/` |
| T7, T8, T14, T17, T18, T19 | `test_rev144_checks.py` |
| T3 (gateway reason), T4, T5, T6, T9, T10, T10a, T11, Review Focus 1 | `test_rev144_entry.py` |
| T12, T16 | `test_rev144_fingerprint.py` |

## Verification (2026-10-29)

- **pytest:** 1632 passed, 0 failed, 0 skipped. Rev.141 had 1595, so Rev.144 adds 37.
- **mypy:** clean under the project config, and clean on `tradepulse/calendar` checked explicitly. Over the whole package: 117 errors, the same as `main`.
- **Ruff:** the new modules and tests are clean, and the edited files have no new findings.

## Follow-ups

- **A2:** measure IBKR `tradingHours`/`liquidHours` (G0 item 3), implement `get_calendar` from them, and record per-contract `instrument_hours` for late-close options.
- **Rev.143:** use `session_deadline` for its escalation deadline (§3.8).
