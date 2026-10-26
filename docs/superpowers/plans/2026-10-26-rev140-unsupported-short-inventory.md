# Rev.140: Unsupported Short Inventory — Implementation Plan (for review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** prepared for review. **Not to be executed** until every gate in "Before Task 1" is met.

**Goal:** TradePulse cannot knowingly submit an oversell. An actual broker oversell is preserved whole and quarantined as a short-exposure liability, which TradePulse never trades automatically. Only verified broker evidence, through an operator command, resolves it.

**Architecture:**
- **The gateway:** authorizes every sell at max(0, min(local bound, broker bound)), under a mandatory per-asset execution reservation.
- **Reconciliation:** keeps a durable fill cursor and a durable record of every unaccounted broker execution.
- **Settlement:** turns an oversell's excess into an append-only liability instead of a short lot, and latches `FINANCIAL_INTEGRITY_BLOCKED`.
- **A new `tradepulse/quarantine/` package** owns the fee allocation and recognition (equation C1), the `quarantine resolve` and `adopt-legacy-lot` commands, and the corporate-action refusal.

**Tech Stack:**
- Python 3.12, asyncio, SQLite through `AsyncSQLiteDatabase`;
- pytest with pytest-asyncio (`asyncio_mode = "auto"`);
- `Decimal` for every financial value;
- `SimulatedBroker` and `ManualClock` from Rev.133–135.

**Spec:** `docs/superpowers/specs/2026-10-26-rev140-unsupported-short-inventory-design.md`, rev. 5 (branch `rev140-design`). Section numbers below (§) refer to it.

## Before Task 1 (gates; none is a code step)

1. **The A0 run:** concluded, its 11 criteria decided, and its report and database preserved with SHA-256. The operator has reviewed that evidence.
2. **Independent review** of spec rev. 5, §3.1 to §3.1b, done, with R12 (the simulator faults in Task 1) decided.
3. **Explicit operator authorization** to implement Rev.140.
4. **No soak or verification generation** is running from the main checkout (soak source guard). Work happens in a fresh worktree from main at that moment, branch `rev140`. The design branch is merged into it first, so the spec travels with the code.

**Rev.140 is one Rev.** Each task below is one commit on `rev140`, with its subject ending `(Rev.140 task N)`. Work stops after Task 13 for the operator's review. Merge (fast-forward main, push, zip) happens only after approval.

## Global Constraints

- Every financial value is `Decimal`. JSON is parsed with `parse_float=Decimal`. No `float` reaches a ledger, a bound or a comparison (spec I10).
- No existing ledger row, lot, fill, settlement event or broker payload is ever rewritten. All nine new tables are insert-only, enforced by triggers (spec §3.11, I9).
- The cash key for every broker execution is `fill:cash:<activity id>`, one entry per execution (I1).
- Refusal reasons are exactly the strings in the spec:
  - `INSUFFICIENT_SELLABLE_QUANTITY`
  - `SELLABLE_BELOW_REQUESTED` (an alert, not a refusal)
  - `SELLABLE_SNAPSHOT_UNSTABLE`
  - `INVENTORY_EVIDENCE_INVALID`
  - `UNRECONCILED_BROKER_ACTIVITY`
  - `UNRESOLVED_BROKER_EXECUTION`
  - `ACTIVITY_PAGE_UNPROVEN`
  - `BROKER_OPEN_ORDERS_UNAVAILABLE`
  - `BROKER_POSITIONS_UNAVAILABLE`
  - `BROKER_ACTIVITIES_UNAVAILABLE`
  - `UNSUPPORTED_SHORT_POSITION`
  - `UNEXPLAINED_SHORT_POSITION`
  - `UNSUPPORTED_SHORT_LOT`
  - `LEGACY_SHORT_LOT`
  - `EXECUTION_RESERVATION_REQUIRED`
  - `EXECUTION_RESERVATION_LOST`
  - `SCHEMA_TRIGGER_MISSING`
  - `BROKER_EVIDENCE_CHANGED`
  - `QUARANTINED_INVENTORY`
  - `ACCOUNT_LEASE_HELD`
- **Snapshot attempts:** at most **3**; then refuse (R10).
- **Rounding:** fee shares use `ROUND_DOWN` at the posted fee's own quantum. The final share of a record and pool takes the exact remainder (§3.6).
- **No new broker call** outside the gateway's four reads (§3.1) and `quarantine resolve`'s activity read. **No order** is ever placed by Rev.140 code except through the existing gateway path.
- `BrokerPort` and `OrderState` are unchanged. An order's original quantity and client order ID are read from `OrderState.raw["qty"]` and `raw["client_order_id"]`.
- Every task ends with `.venv/bin/python -m pytest python_tests -q`, `.venv/bin/ruff check tradepulse python_tests` and `.venv/bin/mypy tradepulse` all clean.

## Review Focus

Inputs the spec implies that no task's own tests would otherwise exercise. Each line's test is added to the owning task.

1. **A broker open sell with `qty: null`** (a notional order, from outside TradePulse) → `INVENTORY_EVIDENCE_INVALID`, never treated as 0. *Task 3, `test_notional_open_order_refuses`.*
2. **A crypto position whose broker quantity is below local lots** because an in-asset fee (`CFEE`) has not settled yet → sellable = *P*. The protective exit is reduced, never refused, and never sells above *P*. *Task 5, `test_crypto_cfee_lag_reduces_not_refuses`.*
3. **A restarted runtime finding an open unresolved execution** written by a previous process, with no latch set (latch call lost) → re-latched at startup before any lane runs. *Task 4, `test_startup_relatches_open_unresolved`.*
4. **An option position** (multiplier 100) oversold by 1 contract → the liability's proceeds use the multiplier, and the valuation identity holds to the cent. *Task 6, `test_option_oversell_uses_multiplier`.*
5. **`quarantine resolve` run while the trading runtime holds the account lease** → refused (`ACCOUNT_LEASE_HELD`). It must not race settlement on the same database. *Task 10, `test_resolve_refuses_while_runtime_leased`.*

---

## File Structure

| File | Responsibility |
|---|---|
| `tradepulse/broker/simulated.py` (modify) | Four test faults: `POSITIONS_LAG`, `STEP_BETWEEN`, `ACCEPT_OVERSELL`, `OVERFILL` |
| `tradepulse/persistence/database.py` (modify) | Nine tables, their triggers, `verify_schema_triggers` |
| `tradepulse/persistence/repositories.py` (modify) | Nine repositories on `PersistenceRepositories` |
| `tradepulse/models/quarantine.py` (create) | The nine record dataclasses |
| `tradepulse/persistence/hydration.py` (modify) | Hydrators for the nine records; `SettlementEvent.liability_hold_projected` |
| `tradepulse/execution/sellable.py` (create) | The pure bound: pending set, both bounds, the decision |
| `tradepulse/reconciliation/unresolved.py` (create) | Fill cursor *C*, unresolved executions, outcomes (K1) |
| `tradepulse/reconciliation/coordinator.py` (modify) | Write unresolved rows, advance *C*; I2; `UNEXPLAINED_SHORT_POSITION`; heuristic disabled on quarantined assets |
| `tradepulse/execution/gateway.py` (modify) | Mandatory reservation; four reads with retry; reduce or refuse; no cover path |
| `tradepulse/settlement/engine.py`, `stages.py`, `accounting.py` (modify) | Liability instead of a short lot; closure conservation with *Q*; `project_liability_hold`; flag lists |
| `tradepulse/monitor/coordinator.py` (modify) | No orders on negative or quarantined assets; alerts |
| `tradepulse/valuation.py`, `tradepulse/risk/engine.py` (modify) | Signed valuation and exposure of quarantined records (§3.3) |
| `tradepulse/quarantine/__init__.py` (create) | `quarantined_positions`, the one read of open quarantined inventory used by the gateway, monitor, valuation and risk |
| `tradepulse/quarantine/fees.py` (create) | Signed allocation; recognitions (C1) |
| `tradepulse/quarantine/resolve.py` (create) | `quarantine resolve`; exactly once (§3.7) |
| `tradepulse/quarantine/legacy.py` (create) | Legacy short-lot detection and `adopt-legacy-lot` (§3.9) |
| `tradepulse/quarantine/corporate_actions.py` (create) | The fail-closed corporate-action refusal (§3.10) |
| `tradepulse/session_commands.py` (modify) | `reset-integrity` gates |
| `tradepulse/cli.py` (modify) | `quarantine resolve`, `quarantine adopt-legacy-lot` |
| `python_tests/test_rev140_*.py` (create) | One file per task, named below |
| `docs/rev140-unsupported-short-inventory.md` (create, Task 13) | The Rev record |

---

### Task 1: Simulator test faults (R12)

**Files:**
- Modify: `tradepulse/broker/simulated.py:148-175` (the `Fault` enum and `_FAULT_SCOPE`), and the order-acceptance check near `:1194`
- Test: `python_tests/test_simulated_broker.py`

**Interfaces:**
- Produces:
  - `Fault.POSITIONS_LAG`, `Fault.ACCEPT_OVERSELL` and `Fault.OVERFILL`, used through the existing `SimulatedBroker.inject(fault, times=1)`;
  - `SimulatedBroker.lag_positions(executions: int) -> None`: `get_positions` serves the view from that many executions ago, until `lag_positions(0)`;
  - `SimulatedBroker.step_before(call: str, action: Callable[[], None]) -> None`: runs `action` once, immediately before the next call named `call` (`"get_open_orders"`, `"get_positions"` or `"get_activities"`);
  - `SimulatedBroker.overfill_next(extra: Decimal) -> None`: the next fill of the next order executes `extra` more than ordered;
  - with `ACCEPT_OVERSELL` injected, the `:1194` insufficient-quantity 403 is skipped for the next `times` orders.

- [ ] **Step 1: Write the failing tests**

```python
async def test_positions_lag_serves_previous_view(sim): ...      # fill 4 of 10; lag_positions(1); get_positions().qty == 10; lag_positions(0) -> 6
async def test_step_before_runs_once_before_named_call(sim): ... # action fills order; first get_open_orders sees it terminal; action not re-run
async def test_accept_oversell_skips_insufficient_qty(sim): ...  # held 10, inject(ACCEPT_OVERSELL); place_order(qty=12, SELL) accepted; position -2
async def test_overfill_next_fills_more_than_ordered(sim): ...   # held 10; overfill_next(Decimal("2")); sell 10 -> FILL qty 12; raw order qty "10"
async def test_open_order_raw_carries_qty_and_client_id(sim): ...# raw["qty"] == "10" and raw["client_order_id"] == the request's
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_simulated_broker.py -q -k "lag or step_before or oversell or overfill or raw_carries"`. Expected: FAIL (attributes are missing).
- [ ] **Step 3:** Implement the four faults. Every injected fault is recorded in the simulator's existing fault log (as for Rev.135), so a test can assert that it fired.
- [ ] **Step 4:** Run the same command. Expected: PASS. Then run the full suite, ruff and mypy.
- [ ] **Step 5:** Commit: `test(broker): simulator faults for position lag, interleaving and oversell (Rev.140 task 1)`.

### Task 2: Schema, records and triggers

**Files:**
- Modify: `tradepulse/persistence/database.py`:
  - `SCHEMA`: add nine `CREATE TABLE IF NOT EXISTS`;
  - `EVIDENCE_TABLES`: add the nine;
  - new `QUARANTINE_TABLES` and `verify_schema_triggers`.
- Modify: `tradepulse/persistence/repositories.py:667` (`PersistenceRepositories`).
- Create: `tradepulse/models/quarantine.py`.
- Modify: `tradepulse/persistence/hydration.py`.
- Test: `python_tests/test_rev140_schema.py`.

**Interfaces:**
- Produces:
  - **Tables** (`record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL`, plus the named unique column):
    - `short_exposure_liabilities(fill_id UNIQUE)`
    - `unmanaged_long_inventory(cover_activity_id UNIQUE)`
    - `quarantine_executions`
    - `quarantine_resolutions(resolution_key UNIQUE)`, where the key is `<record_id>|<activity_id>`
    - `quarantine_fee_allocations(allocation_key UNIQUE)`, where the key is `<fee_canonical_id>|<record_id>`
    - `quarantine_fee_recognitions(recognition_key UNIQUE)`, where the key is `<record_id>|<event_id>|<pool>`
    - `legacy_lot_conversions(lot_id UNIQUE)`
    - `unresolved_broker_executions`
    - `unresolved_execution_outcomes`
  - **Triggers:** `quarantine_no_update_<table>` and `quarantine_no_delete_<table>`, each `BEFORE ... BEGIN SELECT RAISE(ABORT, '<table> is append-only'); END`.
  - **Startup check:** `verify_schema_triggers(connection) -> None` raises `DatabaseError("SCHEMA_TRIGGER_MISSING:<name>")` when any trigger is absent, or its `sqlite_master.sql` differs from the expected text. It is called from `AsyncSQLiteDatabase.initialize` after `initialize_identity`.
  - **Models** in `tradepulse/models/quarantine.py`: frozen, slotted dataclasses, validated as in `models/settlement.py`:
    - `ShortExposureLiability(liability_id, fill_id, broker_order_id, trade_intent_id, asset, quantity, sale_price, multiplier, proceeds, opened_at)`
    - `UnmanagedLongInventory(record_id, cover_activity_id, asset, quantity, cost_price, multiplier, opened_at)`
    - `QuarantineExecution(activity_id, kind: Literal["liability_cover","unmanaged_sale"], asset, side, quantity, price, executed_at, raw_sha256)`
    - `QuarantineResolution(record_id, activity_id, quantity, price, realized_pnl, resolved_at)`
    - `QuarantineFeeAllocation(fee_canonical_id, record_id, pool: Literal["opening","cover"], amount)`
    - `QuarantineFeeRecognition(record_id, event_id, pool, amount, recognized_at)`
    - `LegacyLotConversion(lot_id, liability_id, lot_digest, converted_at, reason)`
    - `UnresolvedBrokerExecution(activity_id, asset_key, side, quantity, price, transaction_time, raw_sha256, first_seen_at, pass_id)`
    - `UnresolvedExecutionOutcome(activity_id, outcome: Literal["matched_fill","quarantine_execution"], matched_record_id, evidence_sha256, resolved_at)`
  - **Repositories:** `PersistenceRepositories` gains attributes with the table names, each a `RecordRepository`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_nine_tables_exist(db): ...                       # names == QUARANTINE_TABLES
async def test_update_and_delete_refused_on_every_table(db): ...# parametrized over 9 tables x {UPDATE, DELETE}; sqlite3 raises "append-only"
async def test_missing_trigger_refused_at_initialize(tmp_path): # drop one trigger, re-initialize -> DatabaseError "SCHEMA_TRIGGER_MISSING:quarantine_no_update_quarantine_executions"
async def test_backup_restore_keeps_triggers(tmp_path): ...     # sqlite3 backup API to new file; verify_schema_triggers passes on it
async def test_new_tables_mark_ever_evidence(db): ...           # insert one liability -> verification_identity.ever_evidence == 1
async def test_models_reject_float_and_nonpositive(db): ...     # ShortExposureLiability(quantity=2.0) raises; quantity=Decimal(0) raises
async def test_existing_database_gains_tables_without_change(tmp_path): # copy of a pre-Rev.140 fixture: every existing table's rows byte-identical after initialize
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_schema.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Trigger SQL is generated from one template, so the expected text in `verify_schema_triggers` is the same string.
- [ ] **Step 4:** Run it. Expected: PASS. Then the full suite, ruff and mypy.
- [ ] **Step 5:** Commit: `feat(persistence): append-only quarantine tables with verified triggers (Rev.140 task 2)`.

### Task 3: The sellable bound (pure)

**Files:**
- Create: `tradepulse/execution/sellable.py`
- Test: `python_tests/test_rev140_sellable.py`

**Interfaces:**
- Consumes: `OrderState` (`broker_order_id`, `status`, `filled_qty`, `raw`), `TradeIntent`.
- Produces:
  - `PendingSell(key: str, original_quantity: Decimal, trade_intent_id: str | None, approved_quantity: Decimal | None, settled_quantity: Decimal)`. `key` = broker order ID, or `intent:<trade_intent_id>` before submission.
  - `SellableEvidenceError(reason: str)`.
  - `build_pending_set(asset_key: str, open_orders_1: Sequence[OrderState], open_orders_2: Sequence[OrderState], intents: Sequence[TradeIntent], settled: Mapping[str, Decimal]) -> tuple[PendingSell, ...]`:
    - it raises `SellableEvidenceError("SELLABLE_SNAPSHOT_UNSTABLE")` if any order of the asset differs between the two reads in `status` or `filled_qty`;
    - and `("INVENTORY_EVIDENCE_INVALID")` for a missing or unparsable `raw["qty"]`, or `filled_qty > qty`.
  - `SellableInputs(broker_quantity: Decimal, local_long_quantity: Decimal, pending: tuple[PendingSell, ...])`.
  - `SellableDecision(sellable: Decimal, local_bound: Decimal, broker_bound: Decimal)`.
  - `compute_sellable(inputs: SellableInputs) -> SellableDecision`:
    - *B*ₗ = *L* − Σ over TradePulse members of (*a* − *s*);
    - *B*ᵦ = *P* − Σ over all members of *q*;
    - sellable = max(0, min(*B*ₗ, *B*ᵦ));
    - it raises `INVENTORY_EVIDENCE_INVALID` if *P* < 0 or *L* < 0.
  - `size_sell(decision: SellableDecision, requested: Decimal, *, protective: bool) -> tuple[Decimal, str | None]`. It returns `(quantity, alert_or_refusal)`:
    - protective and 0 < sellable < requested → `(sellable, "SELLABLE_BELOW_REQUESTED")`;
    - sellable = 0 → `(0, "INSUFFICIENT_SELLABLE_QUANTITY")`;
    - non-protective and requested > sellable → `(0, "INSUFFICIENT_SELLABLE_QUANTITY")`.

  Membership rule: a TradePulse SELL intent is a member until it is terminal **and** fully settled (§3.1, S1). Its broker order, if present in either read, is the same member (matched through `raw["client_order_id"]`), never a second one.

- [ ] **Step 1: Write the failing tests**

```python
def test_partial_fill_delayed_settlement_neither_released_nor_double_counted():
    # L=10, P=6 (4 filled, reflected), intent a=10 s=0, order q=10 filled 4
    d = compute_sellable(SellableInputs(Decimal(6), Decimal(10), (PendingSell("o1", Decimal(10), "i1", Decimal(10), Decimal(0)),)))
    assert (d.local_bound, d.broker_bound, d.sellable) == (Decimal(0), Decimal(-4), Decimal(0))
def test_terminal_order_absent_from_both_reads_stays_member(): ...  # intent filled at broker, unsettled, not in O1/O2 -> one member, a-s = 10
def test_tradepulse_order_counted_once_across_reads_and_intent(): ...# same order in O1, O2 and intent -> len(pending) == 1
def test_submission_unknown_counts_full(): ...                       # s=0 -> local bound L - a
def test_out_of_band_open_order_full_q_in_broker_bound(): ...        # no intent; q=3 -> broker bound P-3; local bound unaffected
def test_reads_disagree_raises_unstable(): ...                       # filled_qty 2 vs 3
def test_filled_above_qty_invalid(): ...
def test_notional_open_order_refuses(): ...                          # raw["qty"] None -> INVENTORY_EVIDENCE_INVALID  (Review Focus 1)
def test_negative_p_or_l_invalid(): ...
def test_size_sell_reduces_protective_refuses_other(): ...           # (6, 10, protective) -> (6, "SELLABLE_BELOW_REQUESTED"); non-protective -> (0, "INSUFFICIENT_SELLABLE_QUANTITY")
def test_lemma_seeded_interleavings():
    # 10_000 seeded random worlds (fixed seed): true H, outstanding orders, position lag <= unreflected fills;
    # assert compute_sellable(...).sellable <= W for every world in which every order is visible or TradePulse's
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_sellable.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement. Module-level functions only; no I/O.
- [ ] **Step 4:** Run it. Expected: PASS. Then the full suite, ruff and mypy.
- [ ] **Step 5:** Commit: `feat(execution): conservative two-bound sellable inventory (Rev.140 task 3)`.

### Task 4: Fill cursor and unresolved executions (§3.1a)

**Files:**
- Create: `tradepulse/reconciliation/unresolved.py`
- Modify: `tradepulse/reconciliation/coordinator.py:330-468` (`_reconcile_fills`)
- Modify: `tradepulse/session_commands.py:236-290` (`run_reset_integrity`), and the runtime startup in `cli.py`'s `run` path
- Test: `python_tests/test_rev140_unresolved.py`

**Interfaces:**
- Consumes: Task 2's `UnresolvedBrokerExecution`, `UnresolvedExecutionOutcome` and repositories.
- Produces:
  - `FILL_CURSOR_RECORD_ID = "fill_reconciliation_cursor"`, stored in `broker_activity_cursors` (existing table; status `active`).
  - `record_pass(repositories, activities: Sequence[BrokerActivity], page_proven: bool, classify: Callable[[BrokerActivity], Literal["matched","unaccounted"]], pass_id: str, now: datetime) -> str | None`:
    - in one write transaction, it inserts an `unresolved_broker_executions` row for every unaccounted activity not already present;
    - it then advances *C* to the last activity ID;
    - it returns the new *C*, or `None` without advancing when `page_proven` is False.
  - `write_outcome_if_proven(repositories, activity_id: str) -> bool`. It writes an outcome only when:
    - there is an exact-ID `Fill` or `quarantine_executions` row with equal side, quantity, price and time;
    - the raw digest is equal;
    - exactly one `fill:cash:<activity_id>` entry exists.
    It returns whether it wrote.
  - `open_unresolved(repositories, asset_key: str | None = None) -> list[UnresolvedBrokerExecution]`.
  - `fill_cursor(repositories) -> str | None`.
  - `relatch_if_open_unresolved(repositories, now) -> bool`, called at runtime startup before any lane.

- [ ] **Step 1: Write the failing tests**

```python
async def test_external_fill_outlives_lookback_window(runtime): ...   # spec test 4c: record, advance C, clock +25h; open_unresolved(asset) non-empty;
                                                                     # run_reset_integrity refuses though missed_fills_detected == 0
async def test_outcome_only_from_exact_evidence(runtime): ...         # heuristic-equal fill does not write an outcome; exact fill + cash does
async def test_crash_between_record_and_advance_leaves_neither(runtime): ... # spec 4d: exception injected in transaction -> no row, C unchanged
async def test_unproven_page_does_not_advance(runtime): ...           # spec 4e
async def test_startup_relatches_open_unresolved(runtime): ...        # Review Focus 3: session ACTIVE + open row -> relatch -> FINANCIAL_INTEGRITY_BLOCKED
async def test_k1_every_activity_at_or_before_c_accounted_or_recorded(runtime): # 50 seeded activities, mixed; invariant holds after each pass
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_unresolved.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. `_reconcile_fills` keeps its existing `fill_lookback` window for matching, unchanged. In addition it reads activities after *C*, using A0's `page_token` completeness, and calls `record_pass`. `run_reset_integrity` refuses with `UNRESOLVED_BROKER_EXECUTION` while `open_unresolved()` is non-empty.
- [ ] **Step 4:** Run it. Expected: PASS. Then the full suite, ruff and mypy.
- [ ] **Step 5:** Commit: `feat(reconciliation): durable fill cursor and unresolved executions (Rev.140 task 4)`.

### Task 5: Gateway sell authorization (§3.1, §3.1b)

**Files:**
- Modify: `tradepulse/execution/gateway.py:200-560`
- Test: `python_tests/test_rev140_gateway.py`

**Interfaces:**
- Consumes:
  - Task 3: `build_pending_set`, `compute_sellable`, `size_sell`, `SellableEvidenceError`;
  - Task 4: `fill_cursor`, `open_unresolved`;
  - Task 2's repositories;
  - `renew_lock` (`persistence/lock.py:64`).
- Produces: `ExecutionGateway.execute_intent` behaviour, in this order for a SELL:
  1. **Token:** `request.symbol_lock_owner_token is None` → `rejected`, `EXECUTION_RESERVATION_REQUIRED`, before any read.
  2. **Reservation:** `renew_lock` fails → `EXECUTION_RESERVATION_LOST`.
  3. **Quarantine:** an open quarantined record or an open unresolved execution on the asset → `UNSUPPORTED_SHORT_POSITION` or `UNRESOLVED_BROKER_EXECUTION`.
  4. **Reads,** up to 3 attempts: *O₁*, *P*, *O₂*, then *A* = activities after *C* for the asset. A read failure → the matching `BROKER_*_UNAVAILABLE`. An unmatched FILL in *A* → `UNRECONCILED_BROKER_ACTIVITY`. Unproven pages → `ACTIVITY_PAGE_UNPROVEN`.
  5. **Size:** `compute_sellable`, then `size_sell(protective=protective_exit)`. A reduction alerts warning `SELLABLE_BELOW_REQUESTED` with both bounds.
  6. **Short guards:** *P* ≤ 0, or a BUY against *P* < 0 → `UNSUPPORTED_SHORT_POSITION`. The `held_quantity < 0` branch of `protective_exit` (`gateway.py:266-267`) is removed.
  7. **Then today's sequence:** `risk_approved`, the fence, `submitted`, submit.

  Every refusal returns `ExecutionResult("rejected", None, [reason], Decimal("0"), None)`, writes no intent row and releases nothing.

- [ ] **Step 1: Write the failing tests**

```python
async def test_sell_without_token_refused_before_reads(runtime): ...       # spec 4i; broker call log empty
async def test_two_protective_exits_at_once_one_reservation(runtime): ...  # spec 4f; asyncio.gather; sum(submitted) <= bound; parametrize positions lag 0/1
async def test_two_gateways_two_connections_same_db(runtime): ...          # spec 4f, cross-connection
async def test_reduced_exit_then_competitor_gets_zero(runtime): ...        # spec 4g
async def test_lease_lost_before_fence_counted_by_competitor(runtime): ... # spec 4h
async def test_seeded_concurrency_never_oversells(runtime): ...            # spec 4j; 200 trials, fixed seed; simulator true long qty >= 0 always
async def test_oversell_accepting_broker_never_reached(runtime): ...       # spec test 2; inject(ACCEPT_OVERSELL, times=1000); 1,000 seeded steps; residual cases latched next reconcile
async def test_terminal_disappearance_with_position_lag(runtime): ...      # spec 4; step_before("get_open_orders", fill_remaining) + lag_positions(1)
async def test_unstable_three_times_refuses_unchanged(runtime): ...        # spec 4b
async def test_external_fill_between_reads_caught_by_activities(runtime): ... # spec 4b
async def test_buy_against_negative_position_refused(runtime): ...         # cover path removed
async def test_crypto_cfee_lag_reduces_not_refuses(runtime): ...           # Review Focus 2
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_gateway.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. The four reads happen before the existing portfolio-risk lock (sells take no portfolio lock today, `gateway.py:285`), inside the caller's execution reservation.
- [ ] **Step 4:** Run it, then `python_tests/test_execution_gateway.py`, `test_runtime_on_simulator.py` and the full suite. Expected: PASS, with no existing test changed except where it relied on a tokenless sell; list each such change in the commit body.
- [ ] **Step 5:** Commit: `feat(execution): sell authorization by conservative bound under mandatory reservation (Rev.140 task 5)`.

### Task 6: Settlement of an oversell (§3.2)

**Files:**
- Modify: `tradepulse/settlement/engine.py:66-119` (`_project_lot`), `:448-470` (handlers)
- Modify: `tradepulse/settlement/stages.py:14-21` (`SETTLEMENT_STAGES`)
- Modify: `tradepulse/settlement/accounting.py:52-110` (`pnl_evidence`), `:200-215` (`accounting_issues` flag list), and the replay flag list
- Modify: `tradepulse/models/settlement.py:41-46`, `tradepulse/persistence/hydration.py:221`
- Test: `python_tests/test_rev140_settlement.py`

**Interfaces:**
- Consumes: Task 2's `ShortExposureLiability` and repository; `latch_financial_integrity_block` (`risk/session.py:190`).
- Produces:
  - `SettlementEvent.liability_hold_projected: bool = False`, hydrated with `d.get("liability_hold_projected", False)`;
  - `SETTLEMENT_STAGES` gains `("liability_hold_projected", "project_liability_hold")` **last**;
  - `COMPLETED` requires both `integrity_verified` and `liability_hold_projected`;
  - `accounting_issues` and replay check all seven flags;
  - `_project_lot`: for a SELL whose plan opens `short` quantity, it calls `repositories.short_exposure_liabilities.create_once(liability_id, liability, unique_value=fill_id)` and creates no `PositionLot`. For a BUY that would close a short lot, it raises `IntegrityViolationError("INTEGRITY_VIOLATION: UNSUPPORTED_SHORT_LOT")`;
  - `pnl_evidence`: the closure check becomes `allocated + liability_quantity != fill.quantity` → `ACCOUNTING_CLOSURE_CONSERVATION_FAILED`;
  - `project_liability_hold(event)`: a no-op without a liability. With one, it calls `latch_financial_integrity_block(reason=f"UNSUPPORTED_SHORT_POSITION: {liability_id}")`, writes an integrity hold on `event.broker_order_id`, and sends one critical alert through `_alert_once`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_oversell_twelve_against_ten(runtime): ...             # spec 5: overfill_next(2); one liability Q=2; no short lot; one cash entry for 12; latched
async def test_closure_conservation_includes_liability(runtime): ... # pnl_evidence passes; removing the liability row -> ACCOUNTING_CLOSURE_CONSERVATION_FAILED
async def test_crash_after_project_lot_and_after_cash(runtime): ...  # spec 7: before_stage raises; resume -> 1 liability, 1 cash, 1 latch, 1 alert
async def test_events_checkpointed_by_c10bb2fa_resume(tmp_path): ... # spec 20: fixture DBs written by main@c10bb2fa at each stage, with/without oversell; ledgers equal a fresh run
async def test_option_oversell_uses_multiplier(runtime): ...         # Review Focus 4
async def test_buy_against_legacy_short_lot_blocked(runtime): ...    # UNSUPPORTED_SHORT_LOT, INTEGRITY_BLOCKED
```

- [ ] **Step 2:** Build the R7 fixtures. Check out `c10bb2fa` in a temporary worktree, run its settlement on the simulator to each stage boundary, and copy the databases to `python_tests/fixtures/rev140/checkpoint_<stage>[_oversell].db` with a `MANIFEST.sha256`. Record the generating script in `python_tests/fixtures/rev140/make_checkpoint_fixtures.py`.
- [ ] **Step 3:** Run `.venv/bin/python -m pytest python_tests/test_rev140_settlement.py -q`. Expected: FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run it, then `test_settlement_engine.py`, `test_durable_accounting.py`, `test_restart_survival.py` and the full suite. Expected: PASS.
- [ ] **Step 6:** Commit: `feat(settlement): oversell excess as quarantined liability, final hold stage (Rev.140 task 6)`.

### Task 7: The monitor (§3.4)

**Files:**
- Modify: `tradepulse/monitor/coordinator.py:446-540`
- Test: `python_tests/test_rev140_monitor.py`

**Interfaces:**
- Consumes: Task 2's repositories; Task 4's `open_unresolved`.
- Produces: in the exit loop, an asset with `position.qty < 0`, an open quarantined record, or an open unresolved execution is `continue`d before `reserve_symbol_for_execution`. It is alerted once per record per UTC day through `_alert_once`, with `event_type` `unsupported_short_position` or `quarantined_inventory`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_negative_position_never_gets_an_order(runtime): ...    # spec 8: 5 passes; broker order log unchanged; 1 alert per day
async def test_quarantined_asset_skipped_other_asset_stop_fires(runtime): ...
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_monitor.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it, then `test_monitor_coordinator.py`, `test_monitor_unmanaged.py` and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(monitor): never trade negative or quarantined inventory (Rev.140 task 7)`.

### Task 8: Valuation, exposure and reconciliation identity (§3.3, §3.5)

**Files:**
- Modify: `tradepulse/valuation.py:200-230`, `tradepulse/risk/engine.py:440-645` (`build_portfolio_snapshot`), `tradepulse/reconciliation/coordinator.py:314-328,362-372`
- Test: `python_tests/test_rev140_valuation.py`

**Interfaces:**
- Consumes: Task 2's records.
- Produces:
  - `quarantined_positions(repositories) -> list[QuarantinedPosition]`, in `tradepulse/quarantine/__init__.py`, where `QuarantinedPosition(asset_key, sign: Literal[-1, 1], open_quantity, multiplier, last_confirmed_mark)`;
  - local equity per §3.3's table, and exposure at max(last confirmed, current);
  - the equity snapshot payload gains `short_exposure_liabilities` and `unmanaged_long_inventory` blocks (open quantity, mark, equity value, unrealized P&L), and `tradepulse/web/app.py`'s snapshot view shows them. Empty lists in normal operation;
  - reconciliation's I2 check per asset: broker qty = open long lots + open unmanaged − open liabilities. A failure, or a negative position or non-zero `short_market_value` not explained by it, latches `UNEXPLAINED_SHORT_POSITION`;
  - `_find_heuristic_match` is not called for an activity whose asset has any quarantined record, or any open unresolved execution.

- [ ] **Step 1: Write the failing tests**

```python
async def test_liability_marks_up_down_and_gap(runtime): ...          # spec 9: +5%, -5%, +20%, -20%; exposure/equity per sign table; broker identity to the cent
async def test_snapshot_blocks_present_and_empty_normally(runtime): ...# blocks exist; [] without quarantine; populated after an oversell
async def test_out_of_band_sell_latches_unexplained_short(runtime): ...# spec 6: no liability until recovery, then exactly one
async def test_cover_not_heuristically_matched_to_unrelated_buy(runtime): ... # spec 15
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_valuation.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it, then `test_valuation_observation.py`, `test_reconciliation_coordinator.py`, `test_risk_engine.py` and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(valuation): quarantined inventory in equity, exposure and reconciliation (Rev.140 task 8)`.

### Task 9: Fee allocation and recognition (§3.6)

**Files:**
- Create: `tradepulse/quarantine/fees.py`
- Modify: `tradepulse/reconciliation/cash_fees.py:14-45`, which receives quarantined records as final allocation targets
- Test: `python_tests/test_rev140_fees.py`

**Interfaces:**
- Consumes: `allocate_cash_fees(proof, attributions)`; Task 2's allocation and recognition records.
- Produces:
  - `allocate_fee(fee_canonical_id: str, signed_amount: Decimal, targets: Sequence[tuple[str, Decimal]], quarantined: Sequence[tuple[str, Decimal, Literal["opening","cover"]]]) -> list[QuarantineFeeAllocation]`. Shares are proportional to the second tuple element (proceeds), `ROUND_DOWN` at the amount's quantum, and the last quarantined target gets the exact remainder. Quarantined targets are always ordered after attributions.
  - `recognize_at_resolution(record_id, pool, open_before: Decimal, covered: Decimal, unrecognized: Decimal, event_id: str, now) -> QuarantineFeeRecognition`. It recognises ⌊*U* × *q* / *q₀*⌋ toward zero, or exactly *U* when *q* = *q₀*.
  - `recognize_adjustment(record_id, pool, amount, event_id, posted_at) -> QuarantineFeeRecognition`.
  - `conservation(repositories, record_id, pool) -> tuple[Decimal, Decimal, Decimal]`, which returns (*A*, *R*, *U*).
  - `check_c1(repositories) -> list[str]`, which returns the records violating *A* = *R* + *U*, or *U* ≠ 0 at open quantity 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_eight_two_on_minus_five_cents(): ...    # spec 10: (-0.04, -0.01); sum == -0.05
def test_two_one_on_minus_one_cent(): ...        # (0.00, -0.01)
async def test_signed_conservation_sequence(runtime): ...
    # spec 11, asserting check_c1(...) == [] after EVERY step:
    # charge; reversal; re-post; correction between partial resolutions (share taken of new U);
    # correction after full resolution -> one dated adjustment; cover fee next evening -> adjustment;
    # same fee ID, different amount -> BROKER_EVIDENCE_CHANGED
async def test_crypto_in_asset_fee_on_excess_holds(runtime): ... # ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_fees.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it, then `test_asset_fees.py`, `test_generation_fees.py` and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(quarantine): signed fee allocation and conserved recognition (Rev.140 task 9)`.

### Task 10: `quarantine resolve` (§3.7, §3.8)

**Files:**
- Create: `tradepulse/quarantine/resolve.py`
- Modify: `tradepulse/cli.py:1108-1160` (a `quarantine` subparser with `resolve`); `tradepulse/settlement/accounting.py` (the replay covers `quarantine_executions`); `tradepulse/session_commands.py` (the `reset-integrity` gates)
- Test: `python_tests/test_rev140_resolve.py`

**Interfaces:**
- Consumes:
  - Tasks 2, 4, 9;
  - `account_lease` (Rev.136): the command takes the account lease, and refuses `ACCOUNT_LEASE_HELD` if the runtime holds it.
- Produces:
  - `resolve_from_activity(repositories, broker: BrokerPort, activity_id: str, reason: str, now: datetime) -> ResolveResult`, where `ResolveResult(resolved: tuple[QuarantineResolution, ...], surplus: UnmanagedLongInventory | None)`;
  - it raises `ResolveRefused(reason)`, writing nothing, for each check in §3.7 step 1;
  - one write transaction inserts:
    - the `QuarantineExecution`;
    - the `CashLedgerEntry("fill:cash:<activity_id>", ...)`, derived exactly like `cash_evidence`;
    - the resolutions, oldest first;
    - the recognitions;
    - the surplus record;
    - an audit event;
  - CLI: `tradepulse quarantine resolve --activity <id> --reason "<text>"`, exit 0 on success, 1 on refusal;
  - `run_reset_integrity` additionally refuses while any quarantined record has open quantity > 0, or `check_c1` is non-empty, or I1 to I5, I7, I8 or I11 fails on the fresh pass.

- [ ] **Step 1: Write the failing tests**

```python
async def test_partial_covers_oldest_first(runtime): ...         # spec 12
async def test_worked_example_200_minus_180_plus_20(runtime): ... # spec 13: cash +200 and -180 (one entry each), realized +20, open 0
async def test_exactly_once_every_recovery_order(runtime): ...   # spec 14: parametrized over the six orders
async def test_surplus_becomes_unmanaged_long(runtime): ...      # spec 16: cover 5 vs liability 2 -> 3 unmanaged; hold stays until verified sale of 3
async def test_refusals_write_nothing(runtime): ...              # spec 17: parametrized over 8 cases; table row counts unchanged
async def test_resolve_refuses_while_runtime_leased(runtime): ...# Review Focus 5
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_resolve.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it, then `test_integrity_reset_proof.py`, `test_cli.py` and the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(quarantine): evidence-verified resolution with exactly-once cash (Rev.140 task 10)`.

### Task 11: Legacy short lots and corporate actions (§3.9, §3.10)

**Files:**
- Create: `tradepulse/quarantine/legacy.py`, `tradepulse/quarantine/corporate_actions.py`
- Modify: `tradepulse/cli.py` (`quarantine adopt-legacy-lot`); lot and holding reads in `settlement/engine.py:66-75,280-290`, which exclude converted lots
- Test: `python_tests/test_rev140_legacy.py`

**Interfaces:**
- Produces:
  - `find_open_short_lots(repositories) -> list[PositionLot]`, latched as `LEGACY_SHORT_LOT: <lot_id>` at startup and on each reconciliation;
  - `adopt_legacy_lot(repositories, broker, lot_id: str, reason: str, now) -> LegacyLotConversion`, which raises `AdoptRefused(reason)` for each of §3.9's four preconditions;
  - `converted_lot_ids(connection) -> frozenset[str]`, used by `_project_lot` and `_project_holding`;
  - on adoption, every fee already attributed to the lot is re-allocated, by appending `opening`-pool `QuarantineFeeAllocation` rows equal to it (Task 9's `allocate_fee`). The original fee attributions are left unchanged, and `check_c1` holds from the conversion onwards;
  - `CORPORATE_ACTION_ACTIVITY_TYPES: frozenset[str]`, taken from Alpaca's documented activity types at implementation, with the source URL and access date in a comment (R9);
  - `corporate_action_blocks(activities, asset_key, since) -> str | None`. Any non-trade activity type naming the instrument that is not known to be harmless (`FILL`, the fee types, `JNLC` and the cash flows) also blocks.

- [ ] **Step 1: Write the failing tests**

```python
async def test_legacy_short_lot_latched_and_adopted(tmp_path): ... # spec 18: fixture; adopt -> liability + conversion; lot and fill bytes unchanged; I2, I3 hold
async def test_adopt_reallocates_existing_fees_c1_holds(tmp_path): ...  # fixture lot with one posted fee; after adopt, check_c1 == [] and original attribution bytes unchanged
async def test_adopt_refuses_lot_with_closures(tmp_path): ...
async def test_adopt_refuses_evidence_database(tmp_path): ...
async def test_unknown_non_trade_activity_blocks_resolution(runtime): ... # spec 17 (corporate action) and R9
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_legacy.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. The test-18 fixture is built by `python_tests/fixtures/rev140/make_legacy_short_fixture.py`, which runs `c10bb2fa`'s settlement on an oversell, so the short lot is authentic old-code output.
- [ ] **Step 4:** Run it, then the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(quarantine): legacy short-lot adoption and fail-closed corporate actions (Rev.140 task 11)`.

### Task 12: Replay of every preserved database (spec test 21)

**Files:**
- Create: `scripts/rev140_replay_check.py`
- Test: `python_tests/test_rev140_replay.py`, which runs on fixtures only and skips nothing

**Interfaces:**
- Produces: `rev140_replay_check.py --database <copy> --report <json>`. It:
  - refuses a path under `data/soaks/`, or any file whose `ever_evidence = 1`, unless it is a copy made by `--copy-from`;
  - takes the copy with SQLite's online backup API, into a 0700 scratch directory;
  - runs `accounting_issues`, `check_c1`, I2 and the settlement replay;
  - writes a report with the source SHA-256 and the per-table digests before and after.

- [ ] **Step 1:** Write `test_replay_fixture_identical` (the checkpoint and legacy fixtures replay to identical digests) and `test_refuses_evidence_path`.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest python_tests/test_rev140_replay.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it. Expected: PASS. Then run the script on a copy of each of the six preserved soak databases and of the service database. Expected: every report has `issues: {}`, identical ledger digests, and no `UNSUPPORTED_SHORT_*`. The original files' SHA-256 values are unchanged before and after.
- [ ] **Step 5:** Commit the script and test. The reports go to `~/tradepulse-evidence/rev140-replay/` with a manifest, never into git. Commit message: `test: replay preserved databases under Rev.140 (Rev.140 task 12)`.

### Task 13: Decimal guard, Rev record, review stop

**Files:**
- Test: `python_tests/test_rev140_decimal.py`
- Create: `docs/rev140-unsupported-short-inventory.md`

- [ ] **Step 1:** Write `test_no_float_in_rev140_paths` (spec test 22). It runs the fixtures of Tasks 3 to 11 with `decimal.getcontext().traps[decimal.FloatOperation] = True`, and asserts every value read from the nine tables, and every `SellableDecision` field, is a `Decimal`.
- [ ] **Step 2:** Run it, then the full suite. Expected: PASS.
- [ ] **Step 3:** Write the Rev record:
  - what changed, per spec section;
  - the behaviour changes (spec §6);
  - the replay report hashes;
  - every test changed in Task 5 and why;
  - anything omitted.
- [ ] **Step 4:** Run `.venv/bin/python -m pytest python_tests -q`, `.venv/bin/ruff check tradepulse python_tests` and `.venv/bin/mypy tradepulse`. Record the counts in the Rev record.
- [ ] **Step 5:** Commit: `docs: Rev.140 record (Rev.140 task 13)`.
- [ ] **Step 6: Stop.** Give the operator the review summary and the zip (`git archive --format=zip --prefix=tradepulse-ai/ -o ~/Downloads/"vegabond-tradepulse(rev.140).zip" <sha>`, sent with SendUserFile). Do not merge until it is approved.

---

## Spec coverage

| Spec | Task |
|---|---|
| §3.1 bound, S1, refusals, tests 1–4b | 3, 5 |
| §3.1a cursor, K1, tests 4c–4e | 4 |
| §3.1b reservation, S2, tests 4f–4j | 5 |
| §3.2 liability, I3, the stage, tests 5, 7, 20 | 6 |
| §3.3 / §3.5 valuation, I2, heuristic, tests 6, 9, 15 | 8 |
| §3.4 monitor, test 8 | 7 |
| §3.6 fees, I4, C1, tests 10, 11 | 9 |
| §3.7 / §3.8 resolve, surplus, I1, I5, I11, tests 12–14, 16, 17 | 10 |
| §3.9 / §3.10 legacy, corporate actions, test 18 | 11 |
| §3.11 schema, triggers, I9, test 19 | 2 |
| R12 simulator faults | 1 |
| Test 21 replay | 12 |
| Test 22, I10 | 13 |
