# Rev.144: shared market calendar, cross-checked against every broker — design specification

**Status:** design, rev. 3. Written from the operator-approved Sections 1–2; rev. 2 corrects joint forensic review findings TP144-01 to 03 (2026-10-27).
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
    def entry_window_open(self, broker: str, asset: AssetIdentity, t: datetime,
                          broker_clock: BrokerClock) -> EntryWindow: ...   # the single entry-authorization question (3.5)
    fingerprint: str

@dataclass(frozen=True, slots=True)
class EntryWindow:
    open: bool
    basis: Literal["venue_regular_session", "verified_instrument_hours", "continuous"] | None
    reason: str | None      # the rejection code when open is False
```

- **`US_EQUITY`:** `exchange_calendars.get_calendar("XNYS")`, so NYSE holidays and early closes. Nasdaq shares the schedule.
- **`US_OPTION`:** XNYS session **days**, with a regular close of 16:00 ET, or 13:00 on early-close days.
  - Per-product late closes, such as 16:15 for some ETF and index options, are **not assumed** and **never change the venue calendar**. They are verified instrument hours (3.4a), used only by `entry_window_open` (3.5) for that instrument.
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

- **When:** at the first reconciliation cycle of each NY date, and at startup.
- **Lifecycle (TP144-R2-01).** Each check is a sequence of **immutable events** in `calendar_check_events`, all keyed by one `check_id` (UUID4):
  1. **`started`**, written and committed **before** any broker call: `check_id`, `broker`, `venue`, the requested range, `started_at`.
  2. **`fetched`**, written after the response arrives: the raw response, `response_sha256`, `received_at`.
  3. Exactly **one terminal event**:
     - **`completed`**, written in **one transaction** with all of that check's `calendar_check_dates` and `instrument_hours` rows. Date rows never exist without their `completed` event;
     - or **`failed`**: a reason, plus the raw error.
  - A `UNIQUE(check_id, event)` constraint, and a check that refuses a second terminal event for a `check_id`, make a check end at most once.
  - **Only a check with a `completed` event confers any verification.** A `started` or `fetched` check without a terminal event (a crash, or an interrupted request) confers nothing.
  - At startup, every check that has had no terminal event for more than 10 minutes gets an appended `failed` event, reason `interrupted`, so the audit trail closes. It never gets `completed`.
- **For each connected broker and each venue it serves:**
  - fetch `get_calendar(today, today + 30 days)`;
  - compare **each date** in the broker's response with `MarketCalendar` for the same venue: session or not, open time, close time.
- **Results are recorded per date (TP144-03),** in the `completed` transaction: one `calendar_check_dates` row per `(check_id, broker, venue, date)`, with result `agreed` or `contested`, plus both values when contested.
  - A date the broker response does **not** contain is not recorded, and is not covered by that check.
  - `checked_at` for freshness is the `completed` event's `received_at`, the time of the broker observation.
- **Status of a (venue, date)** is derived, never stored as a flag:
  - **`contested`:** for any connected broker, the most recent `calendar_check_dates` row for (broker, venue, date) is `contested`.
  - **`verified`:** for **every** connected broker that serves the venue, there is a `calendar_check_dates` row for exactly (broker, venue, date) that is `agreed` and **fresh**. Fresh means its check's `checked_at` falls within the last 3 NYSE trading sessions; that's the freshness policy. A check covers only the dates in its own response. It never verifies a date outside its coverage.
  - **`unverified`:** neither of the above, including any date not covered by a fresh check of every serving broker.
  - **`beyond_horizon`:** past the library window (3.1); it overrides all of the above.
- **On contest:** a critical `CALENDAR_CONTESTED` alert, once per (date, venue, broker), naming both values. A later agreeing row for the same (broker, venue, date) clears it, and both rows are kept. There is no majority vote.
- **On failure to check:** a `failed` event is written, no date rows are written, and status degrades naturally to `unverified` as freshness lapses. A critical `CALENDAR_UNVERIFIED` alert fires once per day while any date in the next 5 sessions is `unverified`.

### 3.4a Verified instrument hours (option late closes)

- **Source:** only the **executing broker's** contract hours for the **instrument's own contract**, e.g. IBKR `reqContractDetails(contract).tradingHours` for that option. A reference contract, another product or another broker never counts.
- **Recorded:** in `instrument_hours` as `(check_id, broker, instrument_key, date, open, close, response_sha256)`, one row per date in the response. Rows exist for the dates the response covers, and nothing else.
- **Valid for an entry** at time *t* when all of these hold:
  - a row exists for (broker, instrument, *t*'s NY date);
  - it is fresh, using the same 3-session policy;
  - its close is after *t*;
  - the **venue** date status is `verified`;
  - the venue has a session that date, and its close is ≤ the instrument close. An instrument row can extend that session's close; it can never create a session on a non-session date.
- **Never changes the venue calendar** or any other instrument.
- **Alpaca:** G0 (3.9) checks whether Alpaca exposes per-contract option hours. If it doesn't, no `instrument_hours` rows exist for Alpaca, and late-close entries on Alpaca stay refused (fail closed).

### 3.5 Use in trading decisions

**Gateway** (`execution/gateway.py`, at the existing non-continuous, non-protective check). The two separate checks, the broker clock and `is_regular_hours`, are replaced by **one** call (TP144-01):

`window = calendar.entry_window_open(broker, asset, now, market_clock)`

`market_clock` is fetched as today, and a fetch failure is still `MARKET_CLOCK_UNAVAILABLE`. The decision, in order:
1. **Crypto:** `open`, basis `continuous`. This is unchanged; the gateway doesn't call it for continuous markets today.
2. **Venue date status** is not `verified` → closed, with `CALENDAR_DATE_CONTESTED`, `CALENDAR_UNVERIFIED` or `CALENDAR_HORIZON_EXCEEDED`.
3. **Venue regular session:** if `now` is within the venue's regular session **and** `market_clock.is_open` → open, basis `venue_regular_session`.
4. **Verified instrument hours:** open, basis `verified_instrument_hours`, when all of these hold:
   - `now` is after the venue's regular close and before a valid instrument-hours close (3.4a);
   - the **venue calendar** shows a session today, with status `verified` (step 2). The broker clock can't answer "did today's session happen", since `BrokerClock` has only `is_open`, `next_open` and `next_close`;
   - the broker clock was fetched successfully and is consistent with "after today's close": `is_open` is false and `next_open > now`;
   - no `CALENDAR_CLOCK_DISAGREEMENT` episode has been active today.

   Here the instrument row is the executing broker's own statement that this contract is still trading, and it stands in for the equity clock, which is closed by then.
5. **Otherwise** closed:
   - `EQUITY_MARKET_CLOSED` or `OPTION_MARKET_CLOSED` when the broker clock says closed, preserving today's strings;
   - `CALENDAR_SESSION_CLOSED` when the clock says open but the calendar does not;
   - `OPTION_INSTRUMENT_HOURS_UNVERIFIED` for an option between the venue close and any claimed late close without a valid row.

**Protective exits and crypto:** unchanged. The calendar is never consulted (C-5).

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
| Option instrument without valid verified hours after the venue close | blocked for that instrument (`OPTION_INSTRUMENT_HOURS_UNVERIFIED`) | allowed | none |

### 3.7 Schema

All tables are append-only, with `BEFORE UPDATE` / `BEFORE DELETE` triggers verified at startup with Rev.140's trigger check (TP144-02).

| Table | Primary key / unique | Columns |
|---|---|---|
| `calendar_check_events` | `UNIQUE(check_id, event)`; at most one of `completed` and `failed` per `check_id` (enforced in the inserting transaction, plus a `BEFORE INSERT` trigger) | `event` (`started`, `fetched`, `completed` or `failed`), `broker`, `venue`, `range_start`, `range_end`, `at`; on `fetched`: `received_at`, `response_sha256`, the raw response; on `failed`: `reason`, the error |
| `calendar_check_dates` | `UNIQUE(check_id, broker, venue, date)`; inserted only in the transaction that writes `completed` for `check_id` | `result` (`agreed` or `contested`), `broker_open`, `broker_close`, `calendar_open`, `calendar_close` |
| `instrument_hours` | `UNIQUE(check_id, broker, instrument_key, date)`; inserted only with `completed` | `open`, `close`, `response_sha256` |

- `check_id` is a UUID4 per check run, so two checks at the same timestamp for different venues or ranges can't collide.
- Statuses are derived only from date and hours rows whose `check_id` has a `completed` event, by the rules in 3.4 and 3.4a.

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
4. Alpaca: whether any endpoint exposes per-contract option trading hours (3.4a). If none does, record that, and Alpaca late-close entries stay refused.

These are recorded in an evidence directory with a manifest. Any difference from the library in (2) or (3) is resolved in the spec before the plan.

## 4. Invariants

- **CAL-I1:** no new stock or option entry is submitted unless `entry_window_open` is open: a verified venue date plus either (the broker clock open and the venue's regular session) or (the instrument's own fresh broker-verified hours, after a verified venue session that day, with a consistent broker clock).
- **CAL-I6:** verification never extends beyond the exact (broker, venue or instrument, date) rows a fresh check produced.
- **CAL-I2:** no protective exit is refused because of the calendar.
- **CAL-I3:** a contested date stays contested until a later successful check agrees. No majority or automatic override.
- **CAL-I4:** the calendar fingerprint is frozen per verification run.
- **CAL-I5:** day counting for the time stop, stall exit and risk day is unchanged.

## 5. Recovery

- The calendar is rebuilt from the pinned library at every start, so there is no state to recover.
- Contested and verified status is re-derived at startup from completed checks' rows (`calendar_check_events`, `calendar_check_dates` and `instrument_hours`).
- A crash during a check leaves at most `started` and `fetched` events, which confer nothing. They are closed as `failed`/`interrupted` at startup, and a new check runs at the next cycle.

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
| T10 | Option late close (TP144-01): an ordinary option at 16:05 → `OPTION_INSTRUMENT_HOURS_UNVERIFIED`; the instrument with a fresh verified 16:15 row → open at 16:05 (basis `verified_instrument_hours`) despite the equity clock reporting closed, and refused at 16:15; another option on the same underlying without its own row → still refused at 16:05; a row from a different broker or a reference contract → not accepted. |
| T10a | Instrument hours never create a session: a row for a calendar holiday → entry refused; on a contested venue date → refused; a stale row (older than 3 sessions) → refused. |
| T11 | Crypto entries and every exit are unaffected in T4–T9. |
| T12 | Fingerprint: changing the library session data (simulated) between run start and a later check → `frozen_configuration_mismatch`; A0's existing context is unaffected. |
| T13 | `get_calendar` shape: Alpaca (recorded fixture), `SimulatedBroker` and an IBKR hours-string fixture return the same `BrokerSession` shape, in UTC. |
| T14 | Immutability of `calendar_check_events`, `calendar_check_dates` and `instrument_hours`: update and delete fail; a missing trigger refuses startup. |
| T15 | Unchanged behaviour: the time stop and stall exit count calendar days; `_risk_day_bounds` is unchanged. |
| T16 | Rev.143 hook: the deadline from `nth_session_after`; contested → the earlier of calendar and weekdays. |
| T17 | Identity (TP144-02): two checks at the same `checked_at` for different venues, and for overlapping ranges, both persist; replaying a check's rows with the same `check_id` changes nothing. |
| T18 | Coverage (TP144-03): a check covering 10-27 to 10-30 does not verify 10-31; a broker response missing a date leaves that date `unverified`; with two connected brokers, one fresh agreement is not enough; a 4-session-old agreement is not fresh. |
| T19 | Lifecycle (TP144-R2-01): a crash after `started` and after `fetched` → no date rows and no verification; startup appends `failed`/`interrupted` after 10 minutes; a second terminal event for a `check_id` is refused; date rows can't be inserted without `completed` in the same transaction; a `failed` check's response never verifies a date. |

## 8. Out of scope

- Extended-hours and overnight trading (no strategy uses them today). Broker windows for them are a later addition.
- Non-US venues.
- Switching the time stop or stall exit to trading days (C-3; a separate, deliberate decision).
- Exchange-specific trading halts in individual symbols. The broker rejects those.

## 9. Open questions

None for the operator. G0 (3.9) may require spec changes before the plan.

## 10. Change log

**rev. 3 (2026-10-27).** Forensic finding TP144-R2-01. The check lifecycle is immutable events (`started` → `fetched` → `completed` | `failed`) in `calendar_check_events`. They replace the `calendar_checks` header, which recorded values it couldn't know at insert time. Date and instrument rows are written only in the `completed` transaction, and only completed checks confer verification. Interrupted checks are closed as `failed` at startup. Test T19.

**rev. 2 (2026-10-27).** Joint forensic review corrections.
- **TP144-01:** one `entry_window_open` decision replaces the separate broker-clock and `is_regular_hours` checks. A late-close option is authorized only from its own executing broker's fresh contract hours, and only after a verified session that day. That also resolves a second contradiction found while fixing it: Alpaca's equity clock reads closed after 16:00. Ordinary contracts stay restricted. Tests T10 and T10a.
- **TP144-02:** check identity is a `check_id` UUID per run, with unique keys over (check, broker, venue[, date]). The original `checked_at` is preserved. Test T17.
- **TP144-03:** evidence and status are per (broker, venue or instrument, date), restricted to a check's actual response coverage. `verified` requires every serving broker's fresh agreement for that exact date. Test T18; invariant CAL-I6.
- G0 adds Alpaca per-contract hours.

**rev. 1 (2026-10-27).** First written version, from approved Sections 1–2.
- Adopts and reshapes Rev.143 rev. 2's `get_calendar` proposal as `BrokerSession`.
- Option late closes are taken from broker contract hours only, never assumed (3.1, 3.4).
- T1 and T2 dates verified against `exchange_calendars` 4.13.2 on 2026-10-27. The library's default window ends 1 year ahead, so construction and the fingerprint use that window (3.1, 3.2).

## 11. Evidence required for implementation approval

- Rev.140 and Rev.141 merged; A0 concluded with its evidence preserved.
- G0 recorded with a manifest.
- All T1–T19 pass; the full Python suite, mypy (project configuration) and Ruff (changed files) are reported exactly.
