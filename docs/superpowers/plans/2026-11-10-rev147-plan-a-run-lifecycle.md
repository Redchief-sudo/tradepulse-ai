# Rev.147 Plan A — Run lifecycle and the dispatch barrier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A verification generation has a frozen planned end. It stops entries durably at `planned_end_at − 30 min`, or on `end-run`. It pauses safely on SIGINT or SIGTERM and resumes only after its checks pass. No BUY can be dispatched past a stop.

**Architecture:**
- **New package `tradepulse/winddown/`.** It holds the policy constants and validation, the append-only `wind_down_events` run-state store, and the dispatch barrier.
- **Freeze** records the `wind_down` policy, and `Verification.check` enforces it.
- **The gateway** refuses entries inside its existing fenced `RISK_APPROVED` transaction. It replaces its `SUBMITTED` write with a `BEGIN IMMEDIATE` compare-and-set that re-reads the run state.
- **The supervisor** writes `run_paused` on a signal, stops scan lanes outside `RUNNING`, and appends `WIND_DOWN_REQUESTED` at the lead time. The wind-down engine that consumes that event is Plan B.

**Tech Stack:** Python 3.12, SQLite through `AsyncSQLiteDatabase`, pytest with asyncio auto mode, the Rev.144 `MarketCalendar`, and `SimulatedBroker`.

**Spec:** `docs/superpowers/specs/2026-11-01-run-wind-down-design.md`, rev. 7 (main `a8b09b21`): §3.1–§3.4, §4.5 freeze check, §6.1, §6.2, §8, and the strings in §11.

## Rev.147 plan roadmap

The spec is split into four plans. Each ends with software that works and is tested. Each is built on its own branch in the `tradepulse-ai-rev147` worktree and reviewed, and only then is the next one started. The whole of Rev.147 merges once, after Plan D and an independent audit.

| Plan | Spec | Deliverable |
|---|---|---|
| **A (this plan)** | §3, §4.5 freeze check, §6, §8 | planned end, run state, pause and resume, `end-run`, the dispatch barrier |
| B: wind-down engine | §4.1–§4.5, §5, §5.1, `wind_down_excluded` | census and observed-order register, classification, crypto close, open-order cancels, fee waits and corroboration, the state machine and invariant, `wind-down resolve` |
| C: handover and registry | §7.1–§7.4, §7.7 | `handover.json` and the seal, the generation registry (with abandon), successor freeze with `--predecessor`, protective-state restore, the carried-lot contract, amendments, bootstrap |
| D: successor start and late fees | §7.5, §7.5.1, §7.6, §7.6.1, the watch carry | start verification, exposure record, continuity class, gap audit and hold, mark evidence and `mark_unverified`, `predecessor_obligation` and the cash bridge, `watched_client_order_ids` |

## Global Constraints

- Fix at the core: no `# type: ignore`, no `cast`, no `assert` used for narrowing. No new `# noqa` without a stated reason.
- Money is `Decimal`. Every timestamp is timezone-aware UTC.
- Commits are GPG-signed (`git commit -S`), with the `Co-Authored-By` trailer.
- Every new refusal string is listed in the spec §11 and in `docs/rev147-record.md`, created in Task 1.
- `WIND_DOWN_LEAD_MINUTES = 30`, `FEE_WAIT_MINUTES = 15`, `CRYPTO_SELL_FEE_WAIT_HOURS = 8`, `SUCCESSOR_START_MARGIN_MINUTES = 30`, `CRYPTO_FEE_RATE = Decimal("0.0025")`. The pre-open exclusion is 15 minutes, the same rule as `scripts/a0_verification_window.py`.
- Never place, cancel or close a broker order outside the gateway. Tests use `SimulatedBroker` or fakes only.
- Build in the `tradepulse-ai-rev147` worktree, on a branch from main. Nothing in the main checkout.
- G3 for each task: `pytest python_tests` green, `mypy` clean with the project config (133+ files), and no ruff findings added to any changed file.

## Review Focus

1. **A crash between `run_paused` and process exit,** or a crash with no event at all. `run --resume` must treat both as `PAUSED`, and must never start scan lanes before every resume check passes. Tested in Task 6.
2. **`end-run` while paused, or a restart after the lead time.** The process must enter wind-down on start and never run a scan lane. Tested in Tasks 6 and 7.
3. **A wall clock that jumps past `planned_end_at − lead`** between watch ticks (a suspended laptop). The transition is still appended once, on the first tick after it. Tested in Task 5.
4. **A BUY in `RISK_APPROVED` when the transition commits,** with its gateway call paused at any await. It is never POSTed. Tested in Task 4.
5. **A generation frozen before Rev.147,** or with the constants changed. It refuses with `generation_predates_wind_down` or `frozen_configuration_mismatch`, and is never migrated. Tested in Task 2.

---

### Task 1: `wind_down_events` and the derived run state

**Files:**
- Create: `tradepulse/winddown/__init__.py`
- Create: `tradepulse/winddown/state.py`
- Create: `docs/rev147-record.md`, the Rev record: scope, plan list, refusal strings, deviations
- Modify: `tradepulse/persistence/database.py`. Add the table to `SCHEMA`. Add `WIND_DOWN_TABLES = ("wind_down_events",)` to `APPEND_ONLY_TABLES`, so it gets `BEFORE UPDATE`/`DELETE` triggers and `verify_schema_triggers` checks it.
- Test: `python_tests/test_rev147_run_state.py`

**Interfaces:**
- Produces:
  - `class RunState(StrEnum)`: `RUNNING`, `PAUSED`, `WINDING_DOWN`.
  - `RUN_EVENTS = frozenset({"run_paused", "run_resumed", "end_run_requested", "WIND_DOWN_REQUESTED"})`. Plan B adds the other wind-down states as events of the same table.
  - `append_event(connection: sqlite3.Connection, *, generation: str, kind: str, cause: str, reason: str | None, now: datetime, census_digest: str | None = None) -> str`. It returns the event ID, and is synchronous, for use inside a caller's transaction.
  - `run_state(connection: sqlite3.Connection, generation: str) -> RunState`. With no events it returns `RUNNING`. A `WINDING_DOWN` state is derived from any `end_run_requested` or `WIND_DOWN_REQUESTED` event, and is never left.
  - `end_requested(connection, generation) -> bool`.
- Table columns: `record_id TEXT PRIMARY KEY, generation TEXT NOT NULL, kind TEXT NOT NULL, cause TEXT NOT NULL, census_digest TEXT, reason TEXT, occurred_at TEXT NOT NULL, seq INTEGER NOT NULL UNIQUE`. `seq` is `MAX(seq)+1` inside the caller's `BEGIN IMMEDIATE`, so ordering never depends on clocks.

- [ ] **Step 1: Write the failing tests**
  - `test_no_events_is_running`.
  - `test_latest_run_event_wins`: `run_paused` → `PAUSED`; then `run_resumed` → `RUNNING`.
  - `test_winding_down_is_terminal`: after `WIND_DOWN_REQUESTED`, appending `run_resumed` raises `ValueError("RUN_WIND_DOWN_TERMINAL")`, and `run_state` stays `WINDING_DOWN`. The same holds after `end_run_requested`.
  - `test_events_are_append_only`: `UPDATE` or `DELETE` on `wind_down_events` raises `sqlite3.IntegrityError`. A database whose trigger was dropped fails `initialize()` with `SCHEMA_TRIGGER_MISSING:`.
  - `test_unknown_kind_is_refused`: `append_event(kind="RUNNING")` raises `ValueError("WIND_DOWN_EVENT_KIND_UNKNOWN: RUNNING")`.
  - `test_generations_do_not_mix`: events of `g1` never change `run_state(c, "g2")`.
- [ ] **Step 2: Run them, and confirm they fail on import.** `pytest python_tests/test_rev147_run_state.py -v`
- [ ] **Step 3: Implement** the table, the triggers and `state.py` with the signatures above. The kinds are validated against `RUN_EVENTS` plus an extension set that Plan B fills.
- [ ] **Step 4: Run them, and confirm they pass.** Also run `pytest python_tests -k "schema or trigger"` to show existing trigger checks still pass.
- [ ] **Step 5: Commit.** `feat(winddown): append-only wind_down_events and the derived run state (Rev.147 A1)`

### Task 2: The frozen planned end

**Files:**
- Create: `tradepulse/winddown/policy.py`
- Modify: `tradepulse/verification/service.py`. In `freeze(...)`, add a keyword-only `end_at: datetime` and a `clock: Callable[[], datetime] = lambda: datetime.now(UTC)`. In `_publish_freeze`, add `"wind_down": wind_down_policy(end_at)` to the policy. In `Verification.check`, add the predates and mismatch checks next to the DBAE check.
- Modify: `tradepulse/verification/commands.py`. Add `--end-at` (ISO with offset), required for `freeze`. A naive value is `wind_down_end_not_aware`.
- Modify: all 15 `freeze(` call sites in `python_tests/`, `tradepulse/` and `scripts/`, which pass `end_at=` from the helper below.
- Test: `python_tests/test_rev147_planned_end.py`

**Interfaces:**
- Produces, in `policy.py`:
  - the constants from Global Constraints;
  - `wind_down_policy(planned_end_at: datetime) -> dict[str, str]`, returning `{"planned_end_at", "lead_minutes", "crypto_sell_fee_wait_hours", "successor_start_margin_minutes", "fee_wait_minutes", "crypto_fee_rate"}`, all as strings;
  - `validate_planned_end(planned_end_at: datetime, now: datetime, calendar: MarketCalendar, *, calendar_verified: bool) -> None`. It raises `VerificationError` with one of:
    - `wind_down_end_not_aware`;
    - `wind_down_end_not_future`;
    - `wind_down_end_inside_session`: inside an XNYS session, or less than 15 min before an open;
    - `wind_down_end_calendar_unverified`: `CalendarHorizonExceeded`, no next session, or `calendar_verified` false;
    - `wind_down_end_fee_window_insufficient`: `planned_end_at` + 30 min + 8 h + 30 min is after the next session's open.
  - `next_session_open(calendar, t) -> datetime`: the first `Session.open` strictly after `t`.
  - `planned_end_at(manifest: dict) -> datetime`.
- Produces, a test helper in `python_tests/rev147_fixtures.py`: `valid_planned_end(now: datetime) -> datetime`. It returns the next weekday XNYS session close plus 30 min, which always passes, and is used by every freeze call site.

- [ ] **Step 1: Write the failing tests.** The calendar is the real `MarketCalendar.from_library()`, and the dates are fixed in 2026.
  - `test_valid_weekday_evening_end`: Wednesday 2026-11-11, 16:30 ET, with `now` = 2026-11-10. It passes, and the next open is 17 h later.
  - `test_refusals` (parametrized):
    - naive datetime → `wind_down_end_not_aware`;
    - equal to `now` → `wind_down_end_not_future`;
    - 11:00 ET on a session day → `wind_down_end_inside_session`;
    - 09:20 ET → `wind_down_end_inside_session`;
    - the fee window. The check is `planned_end_at` + 30 min + 8 h + 30 min (9 h) ≤ Monday 2026-11-16's 09:30 ET open:
      - Monday 00:30 ET exactly → **passes**;
      - Monday 00:31 ET → `wind_down_end_fee_window_insufficient`;
      - Sunday 2026-11-15, 23:00 ET → **passes**;
    - Thanksgiving: Wednesday 2026-11-25, 16:30 ET → passes, with `next_session_open` = 2026-11-27 14:30 UTC (the holiday is skipped). Friday 2026-11-27, 13:30 ET, after the 13:00 early close → passes, not `inside_session`. 12:30 ET that day → `wind_down_end_inside_session`;
    - daylight saving: Monday 2027-03-15, 00:30 EDT → passes (the open is 13:30 UTC). 00:31 EDT → `wind_down_end_fee_window_insufficient`;
    - a date past the library horizon → `wind_down_end_calendar_unverified`;
    - a stale broker cross-check (`CalendarGate.fresh` false) → `wind_down_end_calendar_unverified`.
  - Note: `validate_planned_end` also takes `calendar_verified: bool`. Freeze passes `CalendarGate(...).fresh(checked_at, now)` from the Rev.144 broker cross-check.
  - `test_freeze_records_wind_down_policy`: the `generation` fixture's manifest has `policy["wind_down"] == wind_down_policy(end_at)`, and `check()["integrity_valid"]` is true.
  - `test_pre_wind_down_generation_is_refused`: following `test_dbae_activation.py::pre_dbae_generation`, monkeypatch `freeze_source` to drop `"wind_down"`. Then `check()["reason"] == "generation_predates_wind_down"`, and `invalid.json` is written.
  - `test_changed_constant_is_configuration_mismatch`: monkeypatch `policy.CRYPTO_SELL_FEE_WAIT_HOURS = 9` after the freeze. `check()["reason"] == "frozen_configuration_mismatch"`.
  - `test_cli_requires_end_at`: `verification freeze` without `--end-at` exits non-zero with `wind_down_end_required`.
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement** `policy.py`, the freeze and check wiring, the CLI argument, and the call-site updates.
- [ ] **Step 4: Run the new tests and the full verification suite.** `pytest python_tests -k "verification or freeze or generation or dbae_activation or soak"`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(verification): freeze a planned end and its wind-down policy (Rev.147 A2)`

### Task 3: Entries refused by run state, in the fenced approval

**Files:**
- Modify: `tradepulse/persistence/repositories.py`. Add `update(..., connection_precondition: Callable[[sqlite3.Connection], Sequence[str]] | None = None)`. It runs after `_check_lease` and `_check_precondition`, inside the same transaction, and raises `RepositoryPreconditionError(reasons)` on any reason.
- Modify: `tradepulse/winddown/state.py`. Add `entry_refusals(connection, generation: str | None) -> list[str]`. It returns `[]` for `None` (no generation bound) or `RUNNING`, `["RUN_PAUSED"]` for `PAUSED`, and `["RUN_WIND_DOWN_ACTIVE"]` for `WINDING_DOWN`. Plan D extends it with the per-asset holds.
- Modify: `tradepulse/execution/gateway.py`. The gateway takes `generation: str | None = None`, and `_build_gateway` in `cli.py` passes the verification's generation. For `request.side == Side.BUY`, the `RISK_APPROVED` update passes `connection_precondition=lambda c: entry_refusals(c, generation)`. A refusal is handled like the DBAE precondition: closed unsubmitted, `REJECTED`, with the reason.
- Test: `python_tests/test_rev147_entry_refusal.py`

**Interfaces:**
- Consumes: Task 1's `append_event` and `run_state`.
- Produces: `entry_refusals` as above, and the `connection_precondition` hook on `update`.

- [ ] **Step 1: Write the failing tests.** They use the `test_dbae_gateway.py` rig (`_rig`, `_buy`, `open_market_calendar`), with `generation="g1"` passed to the gateway.
  - `test_paused_run_refuses_a_buy`: append `run_paused`. Execute a BUY. The result is `rejected` with `["RUN_PAUSED"]`, the simulator received no order, and the intent is `REJECTED`.
  - `test_wind_down_refuses_a_buy`: the same with `WIND_DOWN_REQUESTED` gives `RUN_WIND_DOWN_ACTIVE`.
  - `test_protective_sell_is_never_refused`: with `WIND_DOWN_REQUESTED`, a SELL of a held position is accepted and filled.
  - `test_no_generation_never_refuses`: with `generation=None`, a BUY is accepted even with events present.
  - `test_connection_precondition_rolls_back`: a repository unit test. A precondition returning `["X"]` raises `RepositoryPreconditionError` and leaves the row unchanged.
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement** the hook, `entry_refusals` and the gateway wiring.
- [ ] **Step 4: Run them, plus `pytest python_tests/test_dbae_gateway.py python_tests/test_execution_gateway.py`.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(execution): refuse entries by run state inside the fenced approval (Rev.147 A3)`

### Task 4: The dispatch barrier and the transition write

**Files:**
- Create: `tradepulse/winddown/barrier.py`
- Modify: `tradepulse/execution/gateway.py`. Replace the `SUBMITTED` `trade_intents.update(...)` with `dispatch_or_refuse`. Add `self._await_hook: Callable[[str], Awaitable[None]] | None = None`, called as `await self._checkpoint("<name>")` before each await between the `RISK_APPROVED` write and `place_order`. The names are `clock`, `entry_window`, `lease`, `positions`, `quote` and `submitted`. In production the hook is `None` and `_checkpoint` returns at once.
- Test: `python_tests/test_rev147_dispatch_barrier.py`

**Interfaces:**
- Consumes: `entry_refusals` (Task 3).
- Produces:
  - `dispatch_or_refuse(database, *, generation: str | None, approved: TradeIntent, submitted: TradeIntent, now: datetime) -> list[str]`. It runs as one `BEGIN IMMEDIATE`. It re-reads the row and requires `status == "risk_approved"` and a payload equal to `approved`, otherwise `["DISPATCH_INTENT_CHANGED"]` with nothing written. For a BUY, `entry_refusals` mapped to `RUN_PAUSED_AT_DISPATCH` or `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH` (Plan D's hold reasons pass through unchanged) writes the intent `REJECTED` with the first reason, in the same transaction. With no reason it writes `submitted`. It returns `[]` when `SUBMITTED` committed.
  - `append_transition(connection, *, generation, kind, cause, reason, now) -> list[str]`, for `end_run_requested`, `WIND_DOWN_REQUESTED` or `run_paused`. It appends the event, then, in the same transaction, moves every BUY intent in `risk_approved` to `REJECTED` (`RUN_WIND_DOWN_ACTIVE`, or `RUN_PAUSED` for `run_paused`), comparing each full payload. It returns the rejected intent IDs.
  - `async def transition(database, **same) -> list[str]`, which wraps it in `database.run(..., write=True)`.

- [ ] **Step 1: Write the failing tests.** They use the gateway rig with an `_await_hook` that awaits an `asyncio.Event` at a chosen name.
  - `test_transition_at_every_await_never_dispatches` (parametrized over the six names). Pause the BUY at the name, then `transition(kind="WIND_DOWN_REQUESTED")`, then release. Either the result is `rejected` with `RUN_WIND_DOWN_ACTIVE` or `RUN_WIND_DOWN_ACTIVE_AT_DISPATCH` and `rig.broker` received no order, or, for `submitted` only, `SUBMITTED` committed before the transition and the order exists. Assert exactly one of the two, and never an order after a rejected intent.
  - `test_pause_at_every_await`: the same with `run_paused` gives `RUN_PAUSED` or `RUN_PAUSED_AT_DISPATCH`.
  - `test_transition_rejects_risk_approved_buys_in_its_transaction`: insert two `risk_approved` BUY intents and one `risk_approved` SELL. `transition` returns the two BUY IDs. The SELL is untouched.
  - `test_dispatch_refuses_a_changed_intent`: change the stored payload between approval and dispatch. The result is `["DISPATCH_INTENT_CHANGED"]`, the row is unchanged, and no POST is made.
  - `test_protective_sell_dispatches_while_winding_down`: a SELL paused at `quote`, then the transition, then release. The SELL is dispatched and filled.
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement** `barrier.py` and the gateway changes. The R149-03 quote recheck after the `SUBMITTED` commit stays synchronous.
- [ ] **Step 4: Run them, plus the gateway, DBAE and Rev.154 suites.** `pytest python_tests -k "gateway or dbae or dispatch or stranded"`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(winddown): the serialized entry-dispatch barrier and transition write (Rev.147 A4)`

### Task 5: The supervisor: pause on a signal, wind-down at the lead time

**Files:**
- Create: `tradepulse/winddown/supervisor.py`
- Modify: `tradepulse/cli.py`:
  - In `_run_application`, the signal handler calls `pause_on_signal` (scheduled with `asyncio.create_task`, then `shutdown.set()` after it completes or fails).
  - `_scan_action` idles unless `run_state` is `RUNNING`.
  - A `watch_planned_end` task runs alongside `verification.watch`.
- Test: `python_tests/test_rev147_supervisor.py`

**Interfaces:**
- Consumes: `transition` (Task 4), `run_state` (Task 1), `planned_end_at(manifest)` (Task 2).
- Produces:
  - `async def pause_on_signal(database, generation) -> None`, which is `transition(kind="run_paused", cause="signal")`. It is skipped when the state is already `WINDING_DOWN`, because a signal never leaves wind-down.
  - `async def watch_planned_end(database, generation, planned_end: datetime, *, clock, stop: asyncio.Event, tick_seconds: float = 5) -> None`. On each tick it appends `WIND_DOWN_REQUESTED` (cause `planned_end`) once `clock() >= planned_end − 30 min`, or when `end_requested` is true (cause `end_run_requested`). It appends exactly once, then returns.
  - `def scan_allowed(connection, generation) -> bool`.

- [ ] **Step 1: Write the failing tests.**
  - `test_signal_writes_run_paused_and_rejects_approved_buys`.
  - `test_signal_during_wind_down_writes_nothing`.
  - `test_lead_time_appends_wind_down_requested_once`: a `ManualClock` at lead − 1 s, then +2 s, then +1 h. There is exactly one event.
  - `test_clock_jump_past_the_end_still_transitions_once` (Review Focus 3).
  - `test_end_run_request_is_seen_within_one_tick`.
  - `test_scan_lane_idles_outside_running`: `_scan_action` returns `SCAN_IDLE_POLL_SECONDS` without calling the AI provider when the state is `PAUSED` or `WINDING_DOWN`.
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement** `supervisor.py` and the `cli.py` wiring. Monitor, settlement and reconciliation lanes are unchanged.
- [ ] **Step 4: Run them, plus `pytest python_tests -k "run_ or supervisor or lane"`.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(winddown): pause on a signal and request the wind-down at the lead time (Rev.147 A5)`

### Task 6: `run --resume`, with the phase decided from durable state

**Files:**
- Modify: `tradepulse/winddown/supervisor.py`. Add `decide_phase(...)`.
- Modify: `tradepulse/cli.py`, the `_run_application` startup order of §6.2.
- Test: `python_tests/test_rev147_resume.py`

**Interfaces:**
- Produces: `async def decide_phase(database, generation, planned_end: datetime, now: datetime) -> Literal["wind_down", "resume_checks"]`.
  - It returns `"wind_down"` when `now >= planned_end − lead` (appending `WIND_DOWN_REQUESTED` if it is absent), or when the state is `WINDING_DOWN`.
  - Otherwise it returns `"resume_checks"`.
  - After the existing checks pass, `run_resumed` is appended (refused if the state is `WINDING_DOWN`), and only then do scan lanes start.
- Startup order:
  1. load the policy and events;
  2. the existing startup reconciliation (`_run_reconcile`);
  3. `decide_phase`;
  4. on `"resume_checks"`: `verification.check`, `runtime_started` (broker identity), and the reconciliation outcome. Any failure keeps the run `PAUSED`, returns 1, and starts no scan lane.

- [ ] **Step 1: Write the failing tests.**
  - `test_crash_without_event_resumes_only_after_checks`: no events, every check passes. `run_resumed` is appended, and scan lanes start after it.
  - `test_paused_resume_refused_by_identity_change`: the broker reports another account. The state stays `PAUSED`, the result is 1, and no scan lane is started.
  - `test_restart_after_lead_enters_wind_down`: `now = planned_end − 10 min`. `"wind_down"`, `WIND_DOWN_REQUESTED` appended, and no scan lane.
  - `test_restart_with_end_run_requested_enters_wind_down` (Review Focus 2).
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `pytest python_tests -k "resume or run_application or verification"`.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(winddown): resume decides its phase from durable run state (Rev.147 A6)`

### Task 7: `verification end-run` and status

**Files:**
- Modify: `tradepulse/verification/commands.py`. Add the actions `end-run` and `run-status`, and `--reason`.
- Modify: `tradepulse/winddown/supervisor.py`. Add `request_end`.
- Test: `python_tests/test_rev147_end_run.py`

**Interfaces:**
- Produces:
  - `async def request_end(database, generation, reason: str, *, now, os_user: str, calendar) -> dict`.
    - It appends `end_run_requested`, through `transition`, with the reason and the OS user.
    - **Idempotent:** if any end event exists, it appends nothing and returns the current state.
    - Its result is `{"run_state", "planned_end_at", "end_requested_at", "seal_deferred_until"}`. `seal_deferred_until` is the current XNYS session's close when `now` is inside a session, otherwise `None`.
  - **`--reason`** is required: an empty one is `end_run_reason_required`.
  - **`run-status`** returns the same dict and writes nothing.

- [ ] **Step 1: Write the failing tests.**
  - `test_end_run_appends_once`: two calls give one event, and the second returns the existing state.
  - `test_end_run_inside_session_defers_seal`: at 11:00 ET on a session day, `seal_deferred_until` is that day's 16:00 ET close (UTC).
  - `test_end_run_disables_entries_permanently`: after `end-run`, `run_resumed` is refused, and a BUY gives `RUN_WIND_DOWN_ACTIVE`.
  - `test_end_run_requires_reason`.
  - `test_run_status_is_read_only`.
- [ ] **Step 2: Run them, and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, then the full G3:** `pytest python_tests`, `mypy`, and ruff on the changed files. Expected: all green, no new ruff findings.
- [ ] **Step 5: Commit.** Also add every Plan A refusal string to `docs/rev147-record.md`. `feat(verification): end-run and run-status (Rev.147 A7)`

## Self-review

- **Spec coverage, all in Plan A:**
  - §3.1 freeze arguments and refusals: Task 2;
  - §3.2 phases: Tasks 5 and 6;
  - §3.3 signals, end-run, seal deferral: Tasks 5 and 7;
  - §3.4 run state and gateway refusal: Tasks 1 and 3;
  - the §4.5 freeze fee-window check: Task 2;
  - §6.1 storage: Task 1;
  - §6.2 steps 1–3: Task 6. Step 4, the fresh census, is Plan B;
  - §8 steps 1–3 and 5: Task 4.
- **Deferred:**
  - §8 step 4, the observed-order register, is Plan B;
  - §8 step 6, pause and crash, is covered by Tasks 4–6;
  - §8 step 7 is already in Rev.154;
  - the Plan D holds extend `entry_refusals`.
- **Types:**
  - `entry_refusals` returns `list[str]` in Tasks 3 and 4;
  - `transition` and `append_transition` share keyword names;
  - `planned_end_at(manifest)` comes from Task 2 and is used in Tasks 5 and 6.
- **Proportion:** about 300 lines, for spec sections of about 250, with signatures and tests only.
