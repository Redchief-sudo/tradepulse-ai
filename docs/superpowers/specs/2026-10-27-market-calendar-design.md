# Rev.144: shared market calendar, cross-checked against every broker — design specification

**Status:** design, rev. 1, written from the operator-approved Sections 1–2 (2026-10-27).
**Order of work:** Rev.140 → Rev.141 → **Rev.144** → Rev.143 → Rev.142. Rev.143's dividend deadline depends on this spec (section 3.8).
**Branch:** `rev144-calendar-design`. No production code, no migration, and no change to the active A0 verification run.

## 1. What happens today (traced in the code, main at `c10bb2fa`)

- **"Is the market open now?"** is answered only by Alpaca's live clock (`BrokerPort.get_clock`, `AlpacaClock.is_open`):
  - the gateway's submission check (`execution/gateway.py` around lines 476–490: non-continuous markets, non-protective orders);
  - session sync (`risk/session.py` around lines 240–262);
  - the monitor's equity-hours check (`monitor/coordinator.py:357`);
  - the CLI status (`cli.py` around lines 660–672 and 800–808).
- **"Today"** for risk limits is New York midnight to midnight (`risk/engine.py::_risk_day_bounds`).
- **No production code knows trading days, holidays or early closes.** `exchange_calendars` is a **dev-only** dependency (`pyproject.toml` line 17), used by `SimulatedBroker`'s `SimSession.from_exchange_calendar`.
- **Day counting:** the time stop and stall exit (`monitor/coordinator.py::_time_stopped`, `plan_ladder_exit`) count calendar days.
- Rev.143 rev. 2 proposed `get_calendar` on the broker port, with a weekday fallback, for its dividend deadline.
- The A2 spec (IBKR) plans an `IbkrClock` from contract trading hours "cross-checked with the exchange calendar; a disagreement means closed". No such calendar exists yet.

## 2. Requirements and decisions

| ID | Decision |
|---|---|
| C-1 | One broker-independent calendar, keyed by **venue**, not by broker. Alpaca and IBKR route US stocks and options to the same exchanges with the same holidays. |
| C-2 | Source of truth: the `exchange_calendars` library, **pinned**, cross-checked daily against each connected broker's own calendar (operator). |
| C-3 | The calendar is used only for **new uses**: the dividend deadline, broker cross-checks and a second witness on entries. The time stop and stall exit keep counting calendar days, and the risk day stays New York midnight (operator). |
| C-4 | The broker's live clock remains the authority for its own broker. The calendar is a second witness. |
| C-5 | New entries need both witnesses to agree. **Protective exits need only the broker clock**: blocking a stop-loss on a calendar disagreement would add risk. |
| C-6 | Any broker disagreement on a date makes that date **contested**, which fails closed for new entries, with a critical alert. |

## 3. Design

### 3.1 Module `tradepulse/calendar/`

```python
class Venue(StrEnum):
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"

@dataclass(frozen=True, slots=True)
class Session:
    venue: Venue
    date: date               # the trading date, America/New_York
    open: datetime           # aware UTC
    close: datetime          # aware UTC; early closes included
    early_close: bool

class MarketCalendar:
    def is_session(self, venue: Venue, day: date) -> bool: ...
    def session(self, venue: Venue, day: date) -> Session | None: ...
    def sessions_between(self, venue: Venue, start: date, end: date) -> list[Session]: ...  # inclusive
    def nth_session_after(self, venue: Venue, t: datetime, n: int) -> Session: ...         # n >= 1; sessions whose date is after t's NY date
    def is_regular_hours(self, venue: Venue, t: datetime) -> bool: ...
    def status(self, venue: Venue, day: date) -> Literal["verified", "contested", "unverified", "beyond_horizon"]: ...
    fingerprint: str
```

- **`US_EQUITY`:** `exchange_calendars.get_calendar("XNYS")`, so NYSE holidays and early closes. Nasdaq shares the schedule.
- **`US_OPTION`:** XNYS session **days**, with a regular close of 16:00 ET, or 13:00 on early-close days.
  - Per-product late closes, such as 16:15 for some ETF and index options, are **not assumed**. They come from the broker's contract hours (3.4). Until a product's hours are known, entries in the 16:00–16:15 window are refused for it.
- **`CRYPTO`:** always open. `is_session` is always true, and `status` is always `verified`.
- **Horizon.** The library knows sessions only up to its built-in horizon. A query beyond it raises `CalendarHorizonExceeded`, and `status` reports `beyond_horizon`. Callers treat that as fail closed (3.5).
- **Construction.** The calendar is built once at startup with the library's default window: 20 years back to **1 year ahead**. Measured 2026-10-27: the last session is 2027-10-27. It is rebuilt at the first cycle of each new NY date, so the horizon moves forward daily. Rev.144 never asks the library to extrapolate beyond its default window; a date past it is `beyond_horizon`.

### 3.2 Packaging and fingerprint

- `exchange_calendars` moves from `[project.optional-dependencies].dev` to the runtime dependencies, **pinned exactly** (`exchange_calendars==4.13.2`, the version measured on 2026-10-27).
- **`fingerprint`** = SHA-256 of a canonical list of the `US_EQUITY` sessions (date, open, close) for `[today, today + 365 days]`, plus the library version.
- `verification/service.py::context_for` adds `market_calendar_fingerprint`. A verification run started after Rev.144 therefore fails `frozen_configuration_mismatch` if the library is upgraded mid-run.
  - The fingerprint is computed for the run's **opening date** and stored. It is not recomputed daily, since the window moves with the date.
  - **A0 is not affected:** it started before Rev.144 and its frozen context has no such field.

### 3.3 Broker port

`get_calendar(start: date, end: date) -> list[BrokerSession]` is added beside `get_clock`. Rev.143 rev. 2's proposal is adopted here, with this shape:

```python
@dataclass(frozen=True, slots=True)
class BrokerSession:
    venue: Venue
    date: date
    open: datetime    # aware UTC
    close: datetime   # aware UTC
    source: str       # e.g. "alpaca:/v2/calendar", "ibkr:tradingHours:<conId>"
```

| Implementation | How |
|---|---|
| `AlpacaClient` | `GET /v2/calendar?start=&end=`; `date`, `open` and `close` are New York local times, converted to UTC. Field format confirmed at G0 (3.9). |
| `SimulatedBroker` | from its `SimSession`. Tests can inject a disagreement, such as an extra holiday or a shifted close. |
| IBKR (A2) | from `reqContractDetails(...).tradingHours` and `liquidHours` for one reference contract per venue (SPY for `US_EQUITY`, an SPY option for `US_OPTION`), parsed with `timeZoneId`. The A2 spec gets a matching edit when it is next revised. |

### 3.4 Daily cross-check

- At the first reconciliation cycle of each NY date, and at startup, for each connected broker:
  - fetch `get_calendar(today, today + 30 days)`;
  - compare each date with `MarketCalendar` for the same venue: whether it is a session, its open time and its close time.
- **Agreement:** a `calendar_checks` row with result `agreed`, plus the response hash and range.
- **Disagreement on any date:** that date becomes **contested** for that venue. A critical `CALENDAR_CONTESTED` alert fires, once per (date, venue, broker), naming both values. The row records `contested` with the details.
  - A later check that agrees clears the contest, and that is recorded.
  - There is no automatic "majority wins": any contest fails closed.
- **Option late closes:** a broker reporting a later close for a specific option product records a `product_hours` entry. That entry allows that product's entries until its close. It never changes the venue calendar.
- **Failure to check:** the last successful check covers its 30-day range for at most **3 trading days**. After that, `status` is `unverified` for that venue until a check succeeds, with a critical `CALENDAR_UNVERIFIED` alert once per day.

### 3.5 Use in trading decisions

**Gateway** (`execution/gateway.py`, at the existing non-continuous, non-protective check):
- **New entry:** after `market_clock.is_open` (unchanged), additionally require:
  - `calendar.is_regular_hours(venue, now)`;
  - `calendar.status(venue, today) == "verified"`.
- Rejections are `CALENDAR_SESSION_CLOSED`, `CALENDAR_DATE_CONTESTED`, `CALENDAR_UNVERIFIED` and `CALENDAR_HORIZON_EXCEEDED`.
- **Protective exits and crypto:** unchanged. The calendar is never consulted (C-5).

**Session sync** (`risk/session.py`): it still transitions only on the broker clock. In addition, a **clock–calendar disagreement** blocks new entries through the same gateway check, using a new in-memory flag set by session sync, and raises a critical `CALENDAR_CLOCK_DISAGREEMENT` alert once per episode. The two cases are:
- the clock says open while the calendar has no session or is outside regular hours;
- the clock says closed for more than **5 minutes** inside a calendar session.

The flag clears when the two agree again.

**Monitor** (`monitor/coordinator.py:357`): unchanged. Exits follow the broker clock.

**Unchanged by decision C-3:** `_time_stopped`, `plan_ladder_exit` (stall) and `_risk_day_bounds`.

### 3.6 Failure handling

| Situation | New entries | Exits | Alert |
|---|---|---|---|
| Date contested by any broker | blocked on that date and venue | allowed | `CALENDAR_CONTESTED`, critical, once per (date, venue, broker) |
| No successful check for more than 3 trading days | blocked | allowed | `CALENDAR_UNVERIFIED`, critical, daily |
| Date beyond the library horizon | blocked | allowed | `CALENDAR_HORIZON_EXCEEDED`, critical, daily |
| Clock–calendar disagreement | blocked while it lasts | allowed | `CALENDAR_CLOCK_DISAGREEMENT`, critical, once per episode |
| Option product with unknown late close, 16:00–16:15 | blocked for that product | allowed | none |

### 3.7 Schema

`calendar_checks`: `record_id` PK = `{broker}:{checked_at}`, with `broker`, `venue`, `range_start`, `range_end`, `response_sha256`, `result` (`agreed`, `contested` or `failed`), `contested_dates`, the raw response, and `checked_at`.
- It is append-only, with `BEFORE UPDATE` / `BEFORE DELETE` triggers, verified at startup with Rev.140's trigger check.
- The current contested set is derived as the latest row per (broker, venue) whose range covers the date.

### 3.8 Effect on other work

- **Rev.143 (rev. 3 edit, on `rev143-dividends-design`).** The escalation deadline uses `MarketCalendar.nth_session_after(US_EQUITY, first_observed_at, 3).close`. Broker `get_calendar` is the cross-check of 3.4, not the deadline's source.
  - When the date's status is `contested`, `unverified` or `beyond_horizon`, the deadline is the **earlier** of the calendar result and 3 weekdays. The alert may come early, but never late.
  - `deadline_source` becomes `market_calendar` or `weekday_bound`.
- **A2 (IBKR):** `IbkrClock` uses `MarketCalendar` as its exchange calendar, and `get_calendar` is implemented from contract hours (3.3). The A2 spec gets a matching edit when it is next revised. That revision is pending its architectural review.
- **Rev.142:** no change.

### 3.9 G0: measure before implementation (read-only)

1. Alpaca `GET /v2/calendar` for 2026-10-27 to 2027-01-31: the field format, the time zone, and how early closes (11-27, 12-24) appear.
2. Compare (1) with `exchange_calendars` 4.13.2 `XNYS` for the same range, and record any difference.
3. IBKR (when TWS or Gateway is available, read-only): `tradingHours` and `liquidHours` for SPY and one SPY option, over the same range. Record the format and how holidays and early closes appear.

These are recorded in an evidence directory with a manifest. Any difference from the library in (2) or (3) is resolved in the spec before the plan.

## 4. Invariants

- **CAL-I1:** no new stock or option entry is submitted unless the broker clock is open **and** the calendar shows a verified regular session at that instant.
- **CAL-I2:** no protective exit is refused because of the calendar.
- **CAL-I3:** a contested date stays contested until a later successful check agrees. No majority or automatic override.
- **CAL-I4:** the calendar fingerprint is frozen per verification run.
- **CAL-I5:** day counting for the time stop, stall exit and risk day is unchanged.

## 5. Recovery

- The calendar is rebuilt from the pinned library at every start, so there is no state to recover.
- Contested and verified status is re-derived from `calendar_checks` at startup.
- A crash during a check leaves no row, and the check is retried at the next cycle.

## 6. Behaviour changes

- New stock and option entries can additionally be refused on a calendar holiday, an early-close afternoon, a contested or unverified date, or during a clock–calendar disagreement. Each refusal has a named reason.
- Option entries in 16:00–16:15 are refused unless the product's late close is known from the broker.
- Exits, crypto, the time stop, the stall exit and the risk day behave exactly as today.
- New critical alerts: `CALENDAR_CONTESTED`, `CALENDAR_UNVERIFIED`, `CALENDAR_HORIZON_EXCEEDED` and `CALENDAR_CLOCK_DISAGREEMENT`.

## 7. Tests (deterministic; `SimulatedBroker` with its clock; no network)

| # | Test |
|---|---|
| T1 | Library sessions: Thanksgiving 2026-11-26 is not a session; 11-27 closes 13:00 ET; 2026-12-24 closes 13:00 ET; 2027-07-05 (observed Independence Day) is not a session. |
| T2 | `nth_session_after`: Wednesday 2026-11-25 with n=3 gives Tuesday 12-01; the early close counts. |
| T3 | Horizon: a date beyond the library's range raises; `status` is `beyond_horizon`; an entry is refused with `CALENDAR_HORIZON_EXCEEDED`. |
| T4 | Gateway: an entry on a calendar holiday with the broker clock (injected) open → `CALENDAR_SESSION_CLOSED`. |
| T5 | Gateway: an entry on a contested date is refused; a protective exit on the same date is submitted. |
| T6 | Gateway: unverified after 3 trading days without a check → entries refused; exits allowed. |
| T7 | Cross-check: the simulated broker injects an extra holiday → contested + one alert; a later agreeing check clears it; both rows are kept. |
| T8 | Cross-check: a shifted close time → contested. |
| T9 | Clock–calendar disagreement: clock closed during a session for 4 minutes → no block; 6 minutes → block + one alert; agreement clears. |
| T10 | Option late close: unknown product → entry at 16:05 refused; after the broker reports 16:15 for that product → allowed until 16:15. |
| T11 | Crypto entries and every exit are unaffected in T4–T9. |
| T12 | Fingerprint: changing the library session data (simulated) between run start and a later check → `frozen_configuration_mismatch`; A0's existing context is unaffected. |
| T13 | `get_calendar` shape: Alpaca (recorded fixture), `SimulatedBroker` and an IBKR hours-string fixture return the same `BrokerSession` shape, in UTC. |
| T14 | `calendar_checks` immutability: update and delete fail; a missing trigger refuses startup. |
| T15 | Unchanged behaviour: the time stop and stall exit count calendar days; `_risk_day_bounds` is unchanged. |
| T16 | Rev.143 hook: the deadline from `nth_session_after`; contested → the earlier of calendar and weekdays. |

## 8. Out of scope

- Extended-hours and overnight trading (no strategy uses them today). Broker windows for them are a later addition.
- Non-US venues.
- Switching the time stop or stall exit to trading days (C-3; a separate, deliberate decision).
- Exchange-specific trading halts in individual symbols. The broker rejects those.

## 9. Open questions

None for the operator. G0 (3.9) may require spec changes before the plan.

## 10. Change log

**rev. 1 (2026-10-27).** First written version, from approved Sections 1–2.
- Adopts and reshapes Rev.143 rev. 2's `get_calendar` proposal as `BrokerSession`.
- Option late closes are taken from broker contract hours only, never assumed (3.1, 3.4).
- T1 and T2 dates verified against `exchange_calendars` 4.13.2 on 2026-10-27. The library's default window ends 1 year ahead, so construction and the fingerprint use that window (3.1, 3.2).

## 11. Evidence required for implementation approval

- Rev.140 and Rev.141 merged; A0 concluded with its evidence preserved.
- G0 recorded with a manifest.
- All T1–T16 pass; the full Python suite, mypy (project configuration) and Ruff (changed files) are reported exactly.
