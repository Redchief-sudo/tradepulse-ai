# Rev.140: unsupported short inventory — design specification

**Status:** revision 6, for independent review. **Design only.**
- This authorization covers design updates, nothing more: no code, schema, test, merge, push or broker order.
- Implementation starts only after:
  - the A0 verification run (Mon 2026-10-26 16:30 ET to Wed 2026-10-28 16:30 ET, 48 hours) has concluded;
  - its report and database are preserved with SHA-256;
  - its evidence has been reviewed;
  - the operator gives explicit authorization.
- Rev.139, its verification evidence and every scheduled Alpaca run are not touched.

**Revision history:**
- **rev. 1** (2026-10-26): first draft.
- **rev. 2** (2026-10-26): R1 to R3 decisions, immutable records, invariants, recovery, 16 tests.
- **rev. 3** (2026-10-26): the institutional audit's TP140-01 to TP140-05, and the operator's R4 to R8.
- **rev. 6** (2026-10-26): TP140-R5-01 (cursor bootstrap, proven by conservation), TP140-R5-02 (late activities: the cursor is never a completeness proof), TP140-R5-03 (obligations survive reservation expiry during submission), and R12 decided by the operator (test-only faults). Contracts, one append-only table (`fill_cursor_bootstrap`) and tests 4k to 4r.
- **rev. 5** (2026-10-26): TP140-R4-01 (unresolved executions survive the cursor; durable records) and TP140-R4-02 (sell authorization serialized per asset; concurrency tests). Contract and test additions only.
- **rev. 4** (2026-10-26): TP140-R3-01. Section 3.1 is replaced by a conservative two-bound authorization that does not assume atomic broker reads. R9 to R11 are decided. Change log in section 10.

**Origin:**
- the operator's decisions C10, D6 and R1 to R8 (2026-10-25 and 2026-10-26);
- the A1 specification (`phase-a`, sections 6.3, 6.7, 6.9 and 12);
- U4: shorts are outside scope.

**Scope:** a narrowly scoped correction to today's single-account Alpaca runtime, on main. Section 9 states how A1 later makes each record per account.

## 1. What happens today (traced in the code)

| Path | Behaviour | File |
|---|---|---|
| Who sends sells | Only the position monitor. The scanner is buy-only. | `monitor/coordinator.py`, `scanner/coordinator.py` |
| Overlapping internal sells | **Already prevented on the monitor path.** The monitor holds the asset's execution reservation and skips the asset if `has_in_flight_intent` finds any intent in `risk_approved`, `submitted`, `accepted`, `partially_filled` or `submission_unknown`. | `monitor/coordinator.py:492-496`, `execution/idempotency.py:14,45-64` |
| Gateway sell check | `requested ≤ broker held quantity` (`INSUFFICIENT_POSITION_TO_SELL`). **Not net** of other sell intents or broker open orders. Crypto protective exits re-read positions immediately before submission. | `risk/engine.py:244-248`, `execution/gateway.py:263-267,530-543` |
| An oversell fill | `plan_signed_lot_fill` closes the long lots and **opens a short lot** with the excess. `_project_holding` sums signed quantities, giving a **negative holding**. No hold. | `settlement/lots.py:38-101`, `settlement/engine.py:66-119,280-323` |
| Closure conservation | `pnl_evidence` requires Σ closure quantity = fill quantity (`ACCOUNTING_CLOSURE_CONSERVATION_FAILED`). Today a short lot opened from the excess goes unchecked here; in Rev.140, the liability must enter this equation. | `settlement/accounting.py:105-106` |
| Fill cash | One cash entry per fill, `record_id` = `fill:cash:<fill_id>`, where `fill_id` = the broker activity ID. `_persist` makes a re-write a no-op, and a different payload is `ACCOUNTING_DURABLE_ROW_CONFLICT`. | `settlement/accounting.py:43-49,112-127` |
| `Fill` identity | `fills.record_id` = broker activity ID. `Fill.trade_intent_id` is required. | `models/fill.py`, `reconciliation/coordinator.py:420` |
| A negative holding | The monitor sets `exit_side = BUY`, and the gateway classifies a buy against a negative position as a protective exit. **The short is covered automatically.** | `monitor/coordinator.py:498-499`, `execution/gateway.py:266-267` |
| Missed-fill detection | Each pass compares every broker FILL activity with local fills: first by exact ID, then, outside a verification generation, **by heuristic** (symbol, side, quantity, price, time window). An unmatched activity with no known intent is a missed fill and latches. | `reconciliation/coordinator.py:314-328,362-468` |
| Fee allocation | An order-level cash fee is allocated across its FIFO closures in proportion to proceeds. Each share is rounded down to the fee's precision; the last share receives the exact remainder. | `reconciliation/cash_fees.py:1-45` |
| Equity identity | `valuation.py` includes `short_market_value` in the broker identity | `valuation.py:210-218` |

**Evidence, and its limit.** No short lot exists in the six preserved soak databases or the service database (all opened read-only). So no *known* historical short lot needs migrating in the inspected databases. That does not remove the need for a recovery path (3.9) and for tests with short lots present (tests 13, 14 and 21).

## 2. Requirements (operator decisions C10, D6, R1 to R8)

1. **Prevention.** TradePulse cannot knowingly submit an oversell. It relies on its own verified inventory, in-flight reservations and broker open orders, each pending order counted exactly once (3.1). A broker rejection is never the safety mechanism (R2).
2. **Preservation.** An actual broker oversell is preserved whole: fill, cash proceeds, identifiers, fees.
3. **Lots.** Only verified long inventory is closed, by the existing lot rules.
4. **The excess** is a quarantined short-exposure liability: never a tradable lot, never strategy-managed.
5. **Visibility.** The liability is included in equity, mark-to-market valuation, reconciliation and conservative risk.
6. **Realized P&L on the excess** waits until the excess is economically closed, or broker evidence establishes its disposition.
7. **No automatic trading** against it.
8. **The hold** stays until the whole exposure is resolved and reconciliation proves every related record agrees.
9. **Partial resolution (R1):** oldest first within the exact account and instrument. Immutable links between each liability and each covering activity. P&L only on the quantity covered.
10. **Fees (R3):** allocated in proportion, in `Decimal`, with deterministic rounding, reconciling exactly to the posted signed amount. Corrections never double count and never change execution facts.
11. **Exactly once (TP140-02):** one authoritative cash effect and one canonical accounting effect per broker execution, whatever the recovery order.
12. **A cover larger than the liability (R4):** the complete authentic execution is preserved. The surplus is quarantined unmanaged long inventory, and the hold cannot clear until that surplus is accounted for under control.
13. **Corporate actions (R5):** no automatic adjustment. A separately reviewed procedure is required; 3.10 states its minimum evidence.
14. **Never:**
    - discard a real execution;
    - fabricate inventory;
    - convert a negative quantity to long;
    - erase it;
    - turn a cover into an ordinary strategy trade.

## 3. Design

### 3.1 Prevention: a conservative bound on sellable inventory (TP140-01, TP140-R3-01)

**Principle.** The broker's reads are not atomic.
- Positions can lag executions.
- An order can change state, or reach a terminal state and leave the open-order list, between any two reads.
- An external order can appear and fill between reads.

So the gateway does **not** try to reconstruct one consistent broker state. It computes two bounds, each conservative under a *different* kind of lag, and authorizes at most the smaller. Neither bound depends on two reads agreeing.

**Inputs.** All are read inside the asset's execution reservation, so no other TradePulse order on the asset can start meanwhile:
1. *O₁* = `get_open_orders()`;
2. *P* = `get_positions()`: the broker quantity for the asset;
3. *O₂* = `get_open_orders()`;
4. *A* = `get_activities("FILL", after_id = C)`, where *C* is the fill-reconciliation cursor of 3.1a, filtered to the asset. The asset's durable unresolved executions, at any age, are also read (3.1a);
5. **from the local store,** in one read transaction:
   - *L* = Σ open long lot quantity;
   - for every TradePulse SELL intent on the asset not yet **terminal and fully settled:** its approved quantity *a*, and its settled quantity *s*;
   - any open quarantined records.

**The order set** *Ω*, keyed by broker order ID, or by intent ID before submission: every sell order in *O₁* ∪ *O₂*, plus every TradePulse SELL intent from input 5, whatever its broker status. An order that went terminal and left both open-order reads is still in *Ω* through its intent, until it is fully settled locally.

**Bound 1, local (exact for TradePulse's own orders):**

  *B*ₗ = *L* − Σ over TradePulse sell intents in *Ω* of (*a* − *s*)

- *L* changes only when TradePulse settles a fill.
- (*a* − *s*) covers each intent's unsettled part: filled-but-unsettled quantity and unfilled remainder alike. A `submission_unknown` intent counts in full, since *s* = 0.
- **The lag it is immune to:** positions and order status. It reads neither.
- **What it cannot see:** external orders and executions.

**Bound 2, broker (covers external orders):**

  *B*ᵦ = *P* − Σ over every order in *Ω* of its **original** quantity *q*

- **Each order counts at its full original quantity, never `qty − filled_qty`.** So it doesn't matter whether *P* already reflects that order's fills:
  - if a fill *f* is reflected in *P*, the bound is *f* lower than necessary, which is safe;
  - if it is not yet reflected, *P* over-states holdings by at most *f*, and the full *q* subtracts it.
- **The lag it is immune to:** position lag and order-status lag, for every order it has seen.
- **What it cannot see:** an execution from an order that appears in neither open-order read and has no TradePulse intent. That is the activity check's job.

**The activity check, for external executions that positions may not yet reflect.**
- If *A* contains any FILL for the asset that has no exact-ID match among local `Fill` and `quarantine_executions` rows, the sell is refused with `UNRECONCILED_BROKER_ACTIVITY`.
- If the asset has any open `unresolved_broker_executions` record, whatever its age and wherever *C* is, the sell is refused with `UNRESOLVED_BROKER_EXECUTION` (3.1a). This is A1 6.6's uncertain-inventory rule: no exit is sent against inventory TradePulse cannot account for.
- The activity page must be proven complete, as in A0; otherwise it is refused with `ACTIVITY_PAGE_UNPROVEN`.

**Authorization:** sellable = max(0, min(*B*ₗ, *B*ᵦ)).

**Lemma (stated for review; proved by the tests in section 7).** Let *H* be the broker's true long quantity when the order is submitted, and *W* = *H* − Σ the true unfilled remainder of every sell order outstanding then. If none of the refusals below fires, then sellable ≤ *W* for every order and execution that is either TradePulse's or visible in any of *O₁*, *O₂* or *A*.
- **TradePulse's orders only:**
  - *L* = *H* − (filled, unsettled buys) + (filled, unsettled sells).
  - Each intent's *a* − *s* = its filled-unsettled part + its unfilled remainder.
  - So *B*ₗ = *H* − Σ unfilled remainder − (filled, unsettled buys) = *W* − (filled, unsettled buys) ≤ *W*.
- **Orders seen in *O₁* ∪ *O₂*:**
  - *P* = *H* + (fills not yet reflected in *P*), and those are ≤ Σ (*q* − remainder) over seen orders. A lagging buy only lowers *P*.
  - So *B*ᵦ = *P* − Σ *q* ≤ *H* − Σ remainder = *W*.
- **External executions seen only in *A*:** refused outright.

**The stated limit.** No pre-trade check can bound an external order *submitted after the last read*: invisible in *O₂* and *A*, and not yet in *P*. That residual is detected afterwards:
- reconciliation, by a missed fill or I2;
- 3.5's latch.

It is excluded operationally by the existing rule that nothing else trades the account.

**Refusals.** None of them writes anything, releases any reservation, or changes any intent:
- a failed read, of any input: `BROKER_OPEN_ORDERS_UNAVAILABLE`, `BROKER_POSITIONS_UNAVAILABLE` or `BROKER_ACTIVITIES_UNAVAILABLE`;
- *O₁* and *O₂* disagreeing on any order of the asset's status or `filled_qty`, still after **3 attempts** (R10): `SELLABLE_SNAPSHOT_UNSTABLE`.
  - Agreement is **not** treated as proof of atomicity; the bound does not rely on it.
  - Disagreement means the evidence is still moving, so the sell waits.
- `filled_qty > qty` on any order, a negative *P*, or a negative *L*: `INVENTORY_EVIDENCE_INVALID`;
- `UNRECONCILED_BROKER_ACTIVITY` or `ACTIVITY_PAGE_UNPROVEN`;
- `UNSUPPORTED_SHORT_POSITION`, which covers:
  - *P* < 0;
  - a buy classified as protective against a negative position;
  - any order on an asset with open quarantined inventory.

  This removes the gateway's cover path.

**Applying the bound to a request:**
- **Non-protective sells:** none exist today. A request above sellable is refused with `INSUFFICIENT_SELLABLE_QUANTITY (bₗ, bᵦ, requested)`.
- **A protective exit** (a stop, the ladder, a time stop, a forced close) is **reduced** to sellable when sellable > 0, rather than refused, so protection is never lost to the bound's conservatism. The difference is alerted `SELLABLE_BELOW_REQUESTED`, with both bounds.
- **Sellable = 0** refuses with the same reason. The monitor retries next pass, and the reservation and intents are unchanged.
- **An unexplained difference** between *P* and *L* beyond Σ of *Ω*'s quantities is recorded for reconciliation every pass. The bound already takes the smaller.

**Invariant S1, restated.**
- Every order in *Ω* contributes exactly once to each bound: at (*a* − *s*) in *B*ₗ if it is TradePulse's, and at its original *q* in *B*ᵦ.
- A TradePulse sell leaves *Ω* only when it is terminal at the broker **and** fully settled locally. A terminal order disappearing from the open-order list therefore never releases anything.
- Settlement status, `filled_qty` and position freshness **never increase** sellable. They only take effect through *L* (settlement) or *P*, each inside its own bound.

**Cost:** four broker reads per sell, instead of today's one or two. Retries add two per attempt. Exits are rare.

**No broker dependency (R2).** Tests 1 to 4b run against a simulator that accepts every oversell. Whether Alpaca rejects one is not tested; a later experiment needs separate operator authorization, an isolated paper account and isolation from the active verification generation.


### 3.1a Unresolved broker executions and the fill cursor (TP140-R4-01)

**Today:**
- Outside a verification generation, fill reconciliation has **no cursor**. Each pass reads FILL activities over a rolling window, `now − fill_lookback`, which defaults to 1 day (`reconciliation/coordinator.py:338,764`).
- `reset-integrity`'s gate is a fresh reconciliation over that same window (`session_commands.py:271-273`).
- So a missed fill more than 24 hours old drops out of both the activity read and `missed_fills_detected`. The latch it set stays, but the evidence that justified it is no longer re-checked.

This is a pre-existing gap in the live runtime, recorded here and closed by this section.

**Durable records.** Two append-only tables, with the 3.11 triggers:
- **`unresolved_broker_executions`**
  - It holds: `activity_id`, asset key, side, quantity, price, `transaction_time`, the raw payload's SHA-256, `first_seen_at`, and the reconciliation pass ID.
  - Reconciliation writes one row the first time it classifies a FILL activity as unaccounted: no exact-ID match to a `Fill` or a `quarantine_executions` row, and no recovery through a known intent.
  - Unique on `activity_id`.
- **`unresolved_execution_outcomes`**
  - It holds: `activity_id`, the outcome (`matched_fill` or `quarantine_execution`), the matched record ID, the evidence digests, and `resolved_at`.
  - Unique on `activity_id`.
  - It is written **only** when an exact-ID match exists with identical side, quantity, price and time, the raw digest is unchanged, and exactly one `fill:cash:<activity id>` entry exists (I1).
  - No command, flag or timeout writes an outcome.
- An unresolved execution is **open** while it has no outcome.

**The cursor *C*.**
- *C* is a durable, per-account fill-reconciliation cursor: an Alpaca activity ID, ordered with A0's `page_token` rule.
- It is stored with the pass that advanced it.
- *C* advances only in the same SQLite transaction that writes either a canonical outcome (an exact match) or an `unresolved_broker_executions` row for **every** FILL activity between the old and new positions, from a page proven complete.

**Invariant K1:** every FILL activity at or before *C* has either an exact-ID accounting match, or an `unresolved_broker_executions` row. So *A* (after *C*) together with the open unresolved records (any age) covers every FILL activity the broker has reported. Nothing falls between them.

**Interaction with the rest of the design:**
- **The latch:** writing an unresolved row and latching `FINANCIAL_INTEGRITY_BLOCKED` happen in the same pass. If the latch call fails after the commit, startup and the next pass re-latch from the open rows (I8 extended).
- **The sell check:** the asset's open unresolved rows refuse every sell of that asset (3.1).
- **`reset-integrity`:** refused while **any** unresolved execution is open, regardless of `fill_lookback`. This closes the 24-hour gap above.
- **Cursor ordering:** a missed fill is recorded before *C* passes it, so a crash leaves either both the row and the cursor advance, or neither.


**Bootstrap of *C* for an existing database (TP140-R5-01).**

*C* never comes into existence by assumption. Neither "start at the latest activity", which would violate K1, nor "start at the oldest one returned", which would trust history the broker may not fully provide, is acceptable. *C* is created only by an explicit operator command:

`tradepulse reconcile bootstrap-fill-cursor --reason "<text>"`

**Preconditions:**
- the account lease is held by the command itself, and no runtime is running;
- the database is not an evidence database (A1 7.2, 4).

**1. Read the whole history.**
- `GET /v2/account` (account number checked against `AccountRef`; `created_at` kept), and `GET /v2/positions`.
- Every activity of **every** type from inception: no `after`, `direction=asc`, paged to exhaustion under A0's page-completeness rule.
- Every page's raw SHA-256 is recorded.

**2. Inception.** The earliest activity's date must be on or after the account's `created_at` date, and the first cash activity must be a funding flow (`CSD` or `JNLC` in). Otherwise the broker's history cannot be shown to start at the account's beginning: `FILL_HISTORY_INCOMPLETE: inception`.

**3. Position conservation, exact, per asset (P1).**
- Σ signed FILL quantity, plus Σ in-asset fee quantity (`CFEE` with a quantity), plus every other quantity-changing activity, must equal the broker's position quantity.
- An activity type that changes quantity in a way the command does not model refuses (`FILL_HISTORY_INCOMPLETE: unmodelled <type>`).
- Missing early activities show up here, as a position the history does not explain.

**4. Cash conservation, exact to the cent (P1).** Σ of every activity's cash effect (FILL notional by side, and `net_amount` for the rest) must equal the broker's `cash`.
- This identity must first be shown read-only on the paper account before Task 4 is implemented (evidence item, §11). If it does not hold exactly, the cause is found and documented first; it is not absorbed by a tolerance.

**5. Classify every FILL activity.** Each gets exactly one of:

| Class | When | Written as |
|---|---|---|
| `matched_fill` | an exact-ID local `Fill` with identical side, quantity, price and time | outcome |
| `pre_database` | `transaction_time` before the database's `verification_identity.created_at`, so it is history TradePulse never held. It is explained by P1 and, for positions still open, by the opening checkpoint's inherited inventory. | outcome, with the identity and checkpoint digests as evidence |
| `legacy_reconciled` | an existing `reconciliation_records` row (`fill`, outcome `matched`/`corrected`) names the activity, even if by the old heuristic | outcome, citing that row's ID and digest. The row itself is unchanged. |
| (none of these) | | an `unresolved_broker_executions` row |

**6. One write transaction stores:**
- every outcome and unresolved row;
- a `fill_cursor_bootstrap` row (append-only, 3.11): account, boundary activity ID, activity count, page digests, the P1 results, the database's SHA-256 beforehand, and the reason;
- *C* = the last activity ID.

**7. Any failure writes nothing.** Until a bootstrap succeeds:
- `run`, `scan` and `monitor` refuse to start (`FILL_CURSOR_NOT_BOOTSTRAPPED`);
- the deployment is rolled back to the Rev.139 build. The new tables are additive, so the old build runs the same database.

Positions are never left half-protected by a Rev.140 runtime that cannot authorize sells.

**Late-arriving activities: *C* is never a completeness proof (TP140-R5-02).**
- **Why it matters:** Alpaca's activity IDs begin with the transaction time (A0, rev. 3). An activity the broker reports late therefore gets an ID **before** *C*, and `after = C` would never return it.
- **No published bound:** Alpaca documents no maximum reporting delay.
- **No usable evidence either:** the preserved databases cannot measure one, because their inboxes were back-filled at each soak's start.

So no time window alone is treated as proof. Every pass:
1. **Incremental read:** activities after *C*.
2. **Overlap read:** activities with `after` = *C*'s transaction time − *Δ*, where *Δ* is a validated setting (the overlap window). It defaults to **7 days**, chosen for cost, not as a proven bound. Results are deduplicated by activity ID.
3. **A late activity** is one found by read 2 or 3 whose ID ≤ *C* and that has no outcome or unresolved row. It is classified in one transaction (outcome or unresolved row), recorded as a `late_activity` reconciliation record with its first-seen time, and alerted critical. K1 is restored before the pass ends.
4. **Daily full re-read:** once per trading day after the close, every activity since the bootstrap boundary, deduplicated the same way. At today's volume (about 150 fills in three months) this is one or two pages.
5. **Conservation P1, the completeness proof.**
   - **When:** every pass for each asset with activity or a pending sell, and for every asset after the daily re-read.
   - **The equation:** bootstrap baseline + Σ classified quantity activities = broker position quantity, exactly.
   - **On a mismatch,** sells of that asset are refused immediately (`ACTIVITY_HISTORY_UNPROVEN`). If it persists on the next pass (positions can lag a new fill by one read), the account latches `FILL_HISTORY_INCOMPLETE:<asset>`.
   - **The cash equation** is checked daily in the same way.

**What this guarantees.** An execution that positions reflect but no read has returned breaks P1. So does one that a read returns late, until it is classified. Neither can pass silently because a cursor moved past its time.

**The A0 run supplies the first real evidence.** Its database keeps each activity page's receipt time (`received_at`, A0's activity cursor). An activity's first observation is the receipt time of the first page that contains it, and its lag is that minus its `transaction_time`. From these the maximum observed lag is measured, and *Δ* is checked against it before implementation (§11). If the preserved database proves not to support this for every activity, that is reported, and the measurement comes instead from a read-only poll log gathered before Task 4.

### 3.1b Sell authorization is serialized per asset (TP140-R4-02)

**Today:**
- The monitor takes the asset's execution reservation before an exit (`reserve_symbol_for_execution`, a non-blocking lease).
- But the gateway checks it only if the caller passes `symbol_lock_owner_token`, and only immediately before submission (`execution/gateway.py:506-522`).
- A sell without a token is not serialized.

**The contract:**
1. **Required.** Every SELL request, and every order on an asset with quarantined inventory, must carry `symbol_lock_owner_token`. Without it: `EXECUTION_RESERVATION_REQUIRED`, before any read.
2. **Verified at the start.**
   - The gateway verifies and renews the reservation (`renew_lock`) **before** reading any 3.1 input. A lost reservation refuses (`EXECUTION_RESERVATION_LOST`).
   - The existing fence before submission stays.
3. **Order of operations,** all while holding the reservation:
   1. verify the reservation;
   2. read the 3.1 inputs;
   3. compute sellable;
   4. reduce or refuse;
   5. persist the intent at `risk_approved`, with its quantity;
   6. fence;
   7. persist `submitted`;
   8. submit.

   The caller releases the reservation only after `execute_intent` returns. By then the intent is durable in a non-terminal or terminal status, so the next holder's *Ω* contains it.
4. **Lease expiry.** If the lease (`SYMBOL_LOCK_TTL_SECONDS`, renewed during execution) is lost at any point before submission, the fence refuses. An intent persisted at `risk_approved` and never submitted stays in *Ω* until it is marked rejected, so it is counted in full by any concurrent authorization.

**Invariant S2:**
- For each asset (in A1, each position key), at most one sell authorization holds the reservation at a time.
- Every authorization's *Ω* contains every sell intent committed before it acquired the reservation.
- **Consequence:** authorizations are totally ordered, and each subtracts the full quantity of every earlier sell that is still unsettled. So Σ quantities submitted ≤ the min bound in force when the first one began, plus only inventory that settlement has since proven. Two protective exits can never both spend the same sellable quantity, whether either was reduced or not.
- **Across processes:** the reservation is a row in the shared database's `locks` table, so it serializes the monitor inside `run` and a standalone `monitor` command alike. The account lease (Rev.136) separately prevents two runtimes on one account.


**Reservation expiry during submission (TP140-R5-03).** A local lease cannot make a broker submission atomic. So the reservation only decides **who may start** an authorization, and never decides whether an obligation exists.

**What exists today:**
- the submission identity is the intent ID, sent as `client_order_id`, and persisted with status `submitted` before `place_order` (`execution/gateway.py:547-555`);
- `run_with_lock_renewal` does **not** cancel the work when a renewal fails (`persistence/lock.py:92-99`), so an in-flight `place_order` and its polling run to completion after the lease is lost;
- an ambiguous response is `submission_unknown`, recovered by `get_order_by_client_order_id`.

**Invariant S3.**
- Once an intent is persisted at `risk_approved`, its obligation (*a* − *s*, counted in full while unsettled) stays in *Ω* until broker evidence proves it terminal **and** settlement completes.
- No lease expiry, process exit, timeout, holder change or failed lookup removes it.
- Recovery by `client_order_id` needs no lease, and can be done by any process.

**Duplicate exits are refused at the gateway, not only in the monitor.** A SELL for an asset with any other TradePulse sell intent in `submitted` or `submission_unknown` is refused (`PRIOR_SUBMISSION_UNRESOLVED`). This is the monitor's `has_in_flight_intent` rule (`monitor/coordinator.py:495`), made a gateway rule. A second holder who acquires an expired reservation therefore cannot place a second exit while the first is unresolved. Even without the rule, S1 and S3 bound it.

**The original holder after losing its lease:**
- it may finish recording the broker's outcome for its own intent. That is evidence-driven and needs no reservation;
- it starts nothing new: the existing renewal-failure callback;
- it never retries a submission.

### 3.2 Settlement of an oversell

`plan_signed_lot_fill` is unchanged; it reports the excess as `opening_quantity` in the opposite direction.
- **`_project_lot`:**
  - A SELL whose plan opens `short` quantity creates **no lot.** It creates one `ShortExposureLiability`, with `create_once` unique on `fill_id`.
  - Its opening facts are immutable: `liability_id`, `fill_id`, `broker_order_id`, `trade_intent_id`, asset, `quantity` *Q*, `sale_price`, multiplier, `proceeds`, `opened_at`.
  - The closing part settles as today.
- **Closure conservation (amended):** `pnl_evidence`'s check becomes Σ closure quantity + liability *Q* = fill quantity (I3). The liability must exist before `project_trade` runs, which holds because `project_lot` is the first stage.
- **Cash:** unchanged. One entry `fill:cash:<fill_id>` for the full fill.
- **Holdings:** unchanged. Open lots are long only.
- **The hold:** a new final stage, `project_liability_hold`, runs after `verify_integrity`. It:
  - latches `FINANCIAL_INTEGRITY_BLOCKED` (`UNSUPPORTED_SHORT_POSITION: <liability_id>`);
  - writes an integrity hold on the broker order;
  - alerts once.
  Each is idempotent.
- **Status is derived:** open quantity = *Q* − Σ resolution quantity (3.7).

### 3.3 Valuation, exposure and risk

**Two measures, by sign, never mixed:**

| Record | Exposure: absolute, the adverse mark for risk | Equity: the adverse mark for equity |
|---|---|---|
| Long lot / holding | today's rule | today's rule (current mark) |
| Short liability (open *q*) | *q* × max(last confirmed, current) | −*q* × max(last confirmed, current) |
| Unmanaged long (3.8, open *q*) | *q* × max(last confirmed, current) | *q* × min(last confirmed, current) |

- **Local equity** = cash + long holdings value + unmanaged long equity value − short liability equity value.
- **Unrealized P&L on an open liability** = *q* × (sale price − current mark) × multiplier. It is shown in the dashboard and in the equity snapshot.
- **Valuation check:** the broker's `short_market_value` = −Σ (*q* × current mark) within the broker's cent rounding (`valuation.py`'s existing tolerance).

### 3.4 The monitor

- An asset with a negative broker position, or any open quarantined inventory, gets **no order of any kind.**
- It is alerted `UNSUPPORTED_SHORT_POSITION` or `QUARANTINED_INVENTORY` once per record per day.
- Other assets continue as today.

### 3.5 Reconciliation

- **Quantity identity (I2):** broker quantity = Σ open long lots + Σ open unmanaged long − Σ open short liability, exactly.
- **An unexplained short:** a negative broker position or a non-zero `short_market_value` that I2 does not explain latches `UNEXPLAINED_SHORT_POSITION`.
- **No quarantined record is fabricated** from a position read. Records are created only from authentic executions.
- **Exact matching only (R8).** For any FILL activity on an asset with open or historical quarantined records, and for every activity of kind `liability_cover`, heuristic matching (`_find_heuristic_match`) is **disabled**. Only an exact activity-ID match counts.

### 3.6 Fees: signed conservation (R3, TP140-03)

**Allocation.** For each posted fee activity *F*, signed: a charge is negative cash, and a reversal or credit is positive:
- **Targets:** the FIFO closure attributions of the order, plus any liability or unmanaged-long record created by that execution.
- **Shares:** proportional to proceeds (for one fill at one price, to quantity). Each share is rounded `ROUND_DOWN` (toward zero) at *F*'s own quantum. **Quarantined records are ordered last, so the final one receives the exact remainder.**
- **I4 (per fee, signed):** Σ allocations of *F* = *F*'s signed amount, exactly.
- **Records:** append-only `quarantine_fee_allocations(fee_canonical_id, record_id, pool, amount)`, unique on `(fee_canonical_id, record_id)`.
  - `pool` is `opening` (the fees of the execution that created the record) or `cover` (the fees of a resolving execution).
  - A replay is a no-op; a different amount for the same fee ID is `BROKER_EVIDENCE_CHANGED`.
- **Corrections and reversals** are new fee activities with their own canonical IDs. They are allocated by the same rule, with their own signs. Original rows are never edited.
- **Crypto in-asset fees** (`CFEE`) follow `reconciliation/asset_fees.py`. Asset units fall on held long inventory first. One that would draw on short excess raises `ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY`, as today, and holds.

**Recognition.** Fees on a quarantined record enter realized P&L only through `quarantine_fee_recognitions(record_id, event_id, pool, amount)`, which is append-only. The event is a resolution, or a dated adjustment. Per record and pool, define:
- *A* = Σ signed allocations (all fee activities, including later corrections);
- *R* = Σ recognitions;
- *U* = *A* − *R*, the unrecognized balance.

**Conservation equation C1, exact, per record and pool:** *A* = *R* + *U*, with *U* = 0 whenever the record's open quantity is 0. Recognition rules:
1. **At a resolution covering *q* of open quantity *q₀* (before it):** recognise ⌊*U*_opening × *q* / *q₀*⌋ toward zero at the quantum. When *q* = *q₀* (the final resolution), recognise **exactly *U*_opening**.
   - The share is taken of the **current unrecognized balance**, not the original *Q*. So a correction that arrives between partial resolutions is spread over the remaining quantity, and nothing is recognised twice.
2. **Cover fees** (`pool = cover`) are linked to the resolving execution. When they post (Alpaca: the next evening, a daily batch), they are recognised immediately in full, by a dated adjustment event on the posting date. That resolution's quantity is already closed.
3. **An allocation arriving while the record's open quantity is 0** (fully resolved): one dated adjustment event recognises it in full on its posting date, so *U* returns to 0. The original resolutions are never restated.
4. **Signs flow through unchanged.** A reversal allocation is positive, so its recognition is positive and reduces recognised expense. ROUND_DOWN toward zero is symmetric, and the final-remainder rule keeps C1 exact whatever the signs.

This replaces rev. 2's I6 ("recognised ≤ allocated"), which could not express reversals or late corrections.

### 3.7 Resolution: controlled reconciliation only (R1, R4, TP140-02)

Only `tradepulse quarantine resolve --activity <broker activity id> --reason "<text>"` resolves quarantined records.

**1. Verification. It refuses, writing nothing, unless all of these hold:**
- the activity is a broker FILL read fresh from activities. Its raw payload is stored with its SHA-256 and must equal any copy already held (`BROKER_EVIDENCE_CHANGED` otherwise);
- its side is opposite to the open records: a BUY for short liabilities, a SELL for unmanaged long;
- it is for the exact instrument, and executed after the oldest open record opened;
- it is not already used by any resolution;
- it has no TradePulse intent;
- no corporate action affects the instrument since the oldest open record (3.10, R5).

**2. Matching, oldest first.** The quantity is applied to the instrument's open records of the opposite side, in `opened_at` then record-ID order.

**3. Surplus (R4).** If the cover is larger than the open short liabilities:
- the surplus becomes an **unmanaged long inventory** record (3.8) created from the same execution, at its price;
- the execution is preserved whole.
The hold cannot clear while the surplus is open. It is resolved by a later verified opposite-side execution through the same command. Adopting it into strategy management is not in Rev.140; it needs a separately reviewed procedure.

**4. One SQLite transaction writes:**
- **The execution.** One `quarantine_executions` row, `record_id` = the broker activity ID: side, quantity, price, `executed_at`, the raw digest, kind `liability_cover` or `unmanaged_sale`.
  - It is **not** a `Fill`: it has no intent, and `Fill.trade_intent_id` is required.
  - It creates no lot, holding, settlement event or strategy record (I7).
- **Its cash effect.** One cash-ledger entry, `record_id` = `fill:cash:<activity id>`. This is the **same namespace** as every fill's cash, so a second path that tried to book the same execution would collide on `record_id`, and `_persist` would make it a no-op or `ACCOUNTING_DURABLE_ROW_CONFLICT`.
- **The resolutions.** One `quarantine_resolutions` row per record touched: record ID, activity ID, quantity, price, realized P&L (excluding fees). Unique on `(record_id, activity_id)`.
- **The recognitions** under C1 rule 1.
- **The surplus record,** if any.
- **An audit event.**

**5. Realized P&L for *q* covered on a liability:** *q* × (sale_price − cover_price) × multiplier, plus the recognitions of 3.6. On an unmanaged long sold: *q* × (sale_price − cover_price) × multiplier.

**Worked example (the audit's).**
- Oversell excess: 2 shares at $100, giving +$200 in the oversell fill's cash.
- Cover: 2 shares at $90 → one cash entry, −$180.
- Realized P&L, before fees: 2 × (100 − 90) = **+$20**.
- Open quantity: 0.
- Every broker execution has one cash entry; Σ cash for the two = +$20 = realized P&L before fees.

**Exactly-once contract (TP140-02, I11).**

| Path that sees the execution | Effect |
|---|---|
| Reconciliation, before the command | Missed fill: latch. It writes no execution and no cash. |
| `quarantine resolve` | The only writer of the execution, its cash, resolutions and recognitions |
| Reconciliation, after the command | Exact-ID match against `quarantine_executions`; the cash entry exists; nothing is written |
| Late-fill recovery | Not applicable. It requires a TradePulse intent, and a cover has none (the command refuses an activity with an intent). |
| Accounting replay (`replay_accounting`) | Extended to re-derive and verify every quarantine execution's cash entry. A difference is `ACCOUNTING_DURABLE_ROW_CONFLICT`; nothing is rewritten. |
| A crash inside the command | One transaction: everything or nothing. A retry completes, or is refused as already used. |
| The command run twice | The second is refused: activity already used. Unique keys make any write a no-op. |

**6. Releasing the hold (R8).**
- `reset-integrity` refuses while any quarantined record has open quantity > 0.
- It also refuses unless a **fresh** reconciliation pass proves, for every FILL activity in scope:
  - an exact-ID match to a `Fill` or a `quarantine_executions` row, with identical side, quantity, price and time;
  - a raw digest equal to the stored one;
  - exactly one `fill:cash:<activity id>` entry, equal to its derivation.
- It also requires I1 to I5, I7, I8 and C1 to hold.
- **No flag clears a missed fill.** The count `missed_fills_detected` is recomputed from evidence on each pass.

### 3.8 Unmanaged long inventory (R4)

`unmanaged_long_inventory` records the surplus of a cover larger than the open liabilities.
- **Immutable opening facts:** record ID, the cover activity ID, asset, quantity, cost price, multiplier, `opened_at`.
- **Open quantity** = quantity − Σ resolutions.
- **It is quarantined like a liability:**
  - valued (3.3), reconciled (I2), and counted in exposure;
  - no orders (3.1, 3.4);
  - the hold stays while it is open;
  - it is resolved only by a verified SELL through `quarantine resolve`.

### 3.9 Legacy short lots: controlled recovery (TP140-04)

A short `position_lots` row found in any database (restored, historical or recovered) is handled as follows.
- **Detection:** startup and reconciliation scan for open short lots. Any one found:
  - latches `FINANCIAL_INTEGRITY_BLOCKED` (`LEGACY_SHORT_LOT: <lot_id>`);
  - and settlement against it raises `INTEGRITY_VIOLATION: UNSUPPORTED_SHORT_LOT`.
- **Recovery command:** `tradepulse quarantine adopt-legacy-lot <lot_id> --reason "<text>"`. Its preconditions, any failure refusing with nothing written:
  1. The lot's `originating_fill_id` is a `Fill` whose broker activity is present in broker activities, with a raw digest equal to any stored copy.
  2. The fill is a SELL, and the lot's `opened_quantity` = fill quantity − Σ that fill's long closures (I3 for the original fill).
  3. The lot has **no closures.** A lot already partly covered is refused, and stays held, pending a separately reviewed procedure: its covers were booked through ordinary lots and need individual re-examination.
  4. The database is not an evidence database (`ever_evidence = 1`, or a preserved soak or verification database). Those are only ever processed on a copy, as in A1 7.2.
- **Effect,** in one transaction:
  - a `ShortExposureLiability` is created from the original fill's facts, with `create_once(fill_id)`;
  - one `legacy_lot_conversions(lot_id, liability_id, lot_digest, converted_at, reason)` row is written, append-only.
- **The lot row and the fill are never rewritten.** Lot and holding reads exclude lots named in `legacy_lot_conversions`, so the projection sees the liability instead. This keeps I2 and I3 true, and the original rows remain as evidence.
- **Then:** the liability follows 3.3 to 3.7. Fees already attributed to the lot are re-allocated by appending opening-pool allocations equal to them. The originals are left as they are, so C1 holds from the conversion onwards.

### 3.10 Corporate actions: fail closed, and the minimum for a future procedure (R5, TP140-05)

**Rev.140 behaviour.**
- A corporate-action activity on an instrument with open quarantined inventory refuses every resolution for it, and keeps the hold.
- This covers a split, reverse split, name or symbol change, merger, spin-off, or stock or cash distribution. For Alpaca, any non-trade activity type naming the instrument, such as `SPLIT`, `NC`, `MA`, `SPIN`, `DIV` or `DIVNRA`; the exact list is confirmed at implementation.
- A broker position whose symbol changed is an unexplained position under I2.

**Minimum requirements for any future adjustment procedure** (reviewed separately; not designed here):
1. **Broker evidence:** the corporate-action activities themselves, stored raw with SHA-256, plus the broker's own announcement record where available (Alpaca corporate-actions announcements).
2. **Positions on both sides:** a complete snapshot before the effective date and after it, both stored.
3. **An exact quantity reconciliation:** new open quantity = old open quantity × ratio, in `Decimal`. Any fractional remainder is matched to a broker cash-in-lieu activity, exactly.
4. **A basis reconciliation:** price per share × ratio⁻¹ with proceeds unchanged, and total basis conserved exactly. Any cash distribution on a short liability (a payment in lieu owed) is matched to its broker debit.
5. **Immutability:** an append-only adjustment record linking old and new record states. No opening fact is edited.
6. **Two steps by the operator:**
   - `propose`, which shows the full before-and-after reconciliation;
   - then `confirm`, with a reason, recorded in a hash-chained audit trail.
7. **Re-validation:** I2, I3, C1 and the valuation check pass on a fresh reconciliation before any later resolution is accepted.

### 3.11 Schema

New payload tables, created with `CREATE TABLE IF NOT EXISTS`. They are additive, with no rebuild:

| Table | Unique | Notes |
|---|---|---|
| `short_exposure_liabilities` | `fill_id` | opening facts |
| `unmanaged_long_inventory` | `(cover_activity_id)` | opening facts |
| `quarantine_executions` | `record_id` = activity ID | covers and unmanaged sales |
| `quarantine_resolutions` | `(record_id, activity_id)` | |
| `quarantine_fee_allocations` | `(fee_canonical_id, record_id)` | signed |
| `quarantine_fee_recognitions` | `(record_id, event_id, pool)` | signed |
| `legacy_lot_conversions` | `lot_id` | |
| `unresolved_broker_executions` | `activity_id` | 3.1a |
| `unresolved_execution_outcomes` | `activity_id` | 3.1a |
| `fill_cursor_bootstrap` | `account_ref` | 3.1a; at most one per account |

**Immutability (R6).**
- SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers reject changes on all ten tables.
- **At startup the runtime verifies that every trigger exists**, with its expected SQL, and refuses to run otherwise (`SCHEMA_TRIGGER_MISSING`).
- Triggers are stored in the database file, so SQLite's online backup copies them. Test 19 proves a restore keeps them.

## 4. Accounting invariants

Each is checked by reconciliation on every pass, and by the tests.

| # | Invariant | A failure means |
|---|---|---|
| I1 | Every broker execution, whether fill, oversell, cover or unmanaged sale, has exactly one cash entry `fill:cash:<activity id>`, equal to its derivation | latch |
| I2 | Per asset: broker quantity = Σ open long lots + Σ open unmanaged long − Σ open short liability, exactly | latch |
| I3 | Per oversell fill: fill quantity = Σ its lot closures + its liability's *Q* (enforced in `pnl_evidence`) | `INTEGRITY_VIOLATION` |
| I4 | Per posted fee activity: Σ signed allocations = its signed amount, exactly | `INTEGRITY_VIOLATION` |
| I5 | Per quarantined record: Σ resolution quantity ≤ original quantity; each `(record, activity)` resolved at most once | `INTEGRITY_VIOLATION` |
| C1 | Per quarantined record and pool: *A* = *R* + *U*, and *U* = 0 when open quantity is 0 | `INTEGRITY_VIOLATION` |
| I7 | No lot, holding, settlement event or strategy record originates from an oversell's excess, a cover, a surplus or a converted lot | `INTEGRITY_VIOLATION` |
| I8 | Any open quarantined record, legacy short lot or open unresolved broker execution ⇒ `FINANCIAL_INTEGRITY_BLOCKED` | re-latch at startup and on every pass |
| I9 | The ten tables are never updated or deleted, and their triggers exist | refusal / `SCHEMA_TRIGGER_MISSING` |
| I10 | Every financial value is `Decimal`; JSON is parsed with `parse_float=Decimal` | test failure |
| I11 | Each broker activity ID maps to at most one of: a `Fill`, a `quarantine_executions` row | `INTEGRITY_VIOLATION` |
| K1 | Every FILL activity at or before the cursor *C* has an exact accounting match or a durable unresolved row; *C* advances only with those writes (3.1a) | refusal to advance; latch |
| S2 | At most one sell authorization per asset holds the execution reservation; each one's *Ω* contains every earlier committed sell intent (3.1b) | `EXECUTION_RESERVATION_REQUIRED` / `_LOST` |
| S3 | An intent's sell obligation leaves *Ω* only on broker evidence of a terminal state plus completed settlement, never on lease state, timeout or process exit; a second sell of the asset is refused while any TradePulse sell is `submitted` or `submission_unknown` (3.1b) | `PRIOR_SUBMISSION_UNRESOLVED` |
| P1 | Bootstrap baseline + Σ classified quantity activities = broker position quantity, per asset, exactly; the cash identity holds daily (3.1a) | sells refused; latch if it persists |
| S1 | Each order in *Ω* counts exactly once in each bound (*a* − *s* locally, original *q* at the broker). A TradePulse sell leaves *Ω* only when terminal **and** fully settled. Settlement status, `filled_qty` and position freshness never raise sellable. (3.1) | refusal |

## 5. Recovery behaviour

| Crash point | On restart |
|---|---|
| After `project_lot` creates the liability, before `project_cash` | Settlement resumes at `project_cash`; `create_once(fill_id)` makes the liability a no-op; I1 holds. |
| After `project_cash`, before `project_liability_hold` | The stage re-runs: one latch, one hold, one alert. I8 also re-latches at startup. |
| A posted fee during an outage | Allocated on the next pass; unique keys make a replay a no-op; C1 rules 2 and 3 recognise as needed |
| A correction between partial resolutions | Allocated; the next resolution's share comes from the new *U* (C1 rule 1) |
| A correction after full resolution | One dated adjustment recognition (C1 rule 3) |
| Inside `quarantine resolve` or `adopt-legacy-lot` | One transaction: everything or nothing. A retry completes, or is refused as already done. |
| A restart with open quarantined records | The monitor skips the asset, the gateway refuses it, and I8 keeps the latch |
| Restore from backup | The tables and triggers are restored with the file (test 19). Executions after the backup point are re-read from broker activities: fills by settlement, covers by re-running `quarantine resolve`. |
| An upgrade with settlement events in progress | `project_liability_hold` is appended after every existing stage. Events checkpointed by **today's** code resume through it (R7, test 20). |

## 6. Behaviour changes in the live runtime

| Change | When it is visible |
|---|---|
| Sellable = max(0, min(local bound, broker bound)) | Every sell. With no pending sell, sellable = min(*P*, *L*), normally equal to today's held quantity. A protective exit above it is reduced and alerted, never refused while sellable > 0. |
| Three extra broker reads per sell (`get_open_orders` twice, `get_activities` once) | Every exit; within the rate limit |
| Sells require the execution reservation | Always. The monitor already passes it, so no change on today's path. |
| A durable fill cursor and unresolved-execution records | Every reconciliation pass; normally no unresolved rows |
| `reset-integrity` refuses while any unresolved execution is open, at any age | Only after a missed fill. Today such a fill ages out of the gate after 24 h. |
| `UNSUPPORTED_SHORT_POSITION` refusals; no automatic cover | Only with negative or quarantined inventory: none in the inspected databases |
| An oversell becomes a liability and a latch | Only on an oversell: none in the inspected databases |
| The closure conservation check includes the liability | Every settlement; identical without an oversell |
| Heuristic fill matching disabled for quarantined assets and covers | Only with quarantined records |
| Ten new tables with triggers; trigger verification at startup | Always; normally empty |
| A new final settlement stage | Every settlement; a no-op without a liability |

Replaying every preserved soak database must give identical ledgers and decisions.

## 7. Tests (deterministic, on `SimulatedBroker` with its clock; no network)

**Prevention (TP140-01, TP140-R3-01).** These need two new Rev.135-style simulator faults:
- `POSITIONS_LAG(n)`: positions served from the view *n* executions ago;
- `STEP_BETWEEN(call, action)`: an execution, cancel or out-of-band order applied immediately before the named read.

1. **Partial fills with delayed settlement:**
   - a sell of 10 against 10 fills 4; *P* = 6 reflected; local settlement is delayed;
   - *B*ₗ = 10 − 10 = 0 and *B*ᵦ = 6 − 10 → 0, so sellable = 0: neither released (6) nor double-counted;
   - after settlement *B*ₗ = 6 − 6 = 0; after the last 6 fill and settle, the intent leaves *Ω*.
2. **Overlapping sells, no broker safety net:**
   - the simulator accepts oversells;
   - two exits race, called directly against the gateway; exactly one is approved;
   - a seeded sequence of 1,000 random steps (sells, partial fills, cancels, out-of-band orders, `POSITIONS_LAG` and `STEP_BETWEEN` at random reads; fixed seed) **never** yields a submitted sell that drives the simulator's true long quantity, net of outstanding sells, below 0, except where the stated limit applies (an out-of-band order placed after the last read). Those cases are asserted to be latched by the next reconciliation.
3. **Order states:**
   - a canceled partial;
   - an expired order;
   - `submission_unknown` (*s* = 0, counted in full);
   - an out-of-band open order (counted at its full *q* in *B*ᵦ, and alerted);
   - `filled_qty > qty` (`INVENTORY_EVIDENCE_INVALID`).
4. **Terminal-order disappearance:**
   - `STEP_BETWEEN(get_open_orders#1, fill-remaining)` together with `POSITIONS_LAG(1)`. The TradePulse order completes and is absent from both *O₁* and *O₂*, while *P* still shows the pre-fill quantity;
   - the order stays in *Ω* through its unsettled intent, *B*ₗ is unchanged, and sellable is not increased.
4a. **Delayed broker positions:**
   - `POSITIONS_LAG(1..3)` across fills, with settlement current;
   - *B*ₗ governs; a protective exit is reduced to sellable and alerted `SELLABLE_BELOW_REQUESTED`, never above the true quantity.
4b. **Inconsistent order and position evidence:**
   - *O₁* ≠ *O₂* on three attempts → `SELLABLE_SNAPSHOT_UNSTABLE`, with no write and reservations unchanged;
   - one mismatch, then agreement → it proceeds, with the bound computed afresh;
   - *P* < 0 or *L* < 0 → `INVENTORY_EVIDENCE_INVALID`;
   - an external FILL in *A* with no local match → `UNRECONCILED_BROKER_ACTIVITY`;
   - an external order that appears and fills between *O₁* and *O₂* (`STEP_BETWEEN`), with *P* lagging → caught by *A*;
   - an incomplete activity page → `ACTIVITY_PAGE_UNPROVEN`.

**Cursor safety (TP140-R4-01).** These use the simulator clock to pass `fill_lookback`:

4c. **The external fill that outlives the window:**
   - an external SELL fill is observed and recorded unresolved, the latch is set, and *C* advances past it as later activities arrive;
   - 25 h later (beyond `fill_lookback`), with no outcome, a protective sell of that asset is refused (`UNRESOLVED_BROKER_EXECUTION`);
   - `reset-integrity` is refused, although `missed_fills_detected` over the window is 0;
   - only after an exact-ID accounted outcome is written does authorization proceed.
4d. **Crash between detection and cursor advance:** neither the unresolved row nor the advance survives, and the next pass records both. A crash after the commit but before the latch is followed by a re-latch at startup.
4e. **The cursor never skips:** with an incomplete activity page, *C* does not advance, and the sell check refuses (`ACTIVITY_PAGE_UNPROVEN`).

**Bootstrap and late activities (TP140-R5-01, R5-02):**

4k. **Bootstrap, complete history:**
   - a simulator account with three months of fills, fees, an inherited position from before the database existed, and one legacy heuristic match;
   - the bootstrap classifies every FILL (`matched_fill`, `pre_database`, `legacy_reconciled`) and sets *C*;
   - P1 holds; the bootstrap record's digests reproduce.
4l. **Bootstrap, missing early history:**
   - the simulator's activity history is truncated before the first fill; positions are unchanged;
   - P1 fails: `FILL_HISTORY_INCOMPLETE`, nothing is written, and `run` refuses `FILL_CURSOR_NOT_BOOTSTRAPPED`;
   - truncating before the funding flow fails the inception check instead.
4m. **Bootstrap refusals:**
   - an unmodelled quantity-changing activity type;
   - a cash identity off by 0.01;
   - an evidence database;
   - a runtime holding the lease.
   Each writes nothing.
4n. **A delayed activity:**
   - a FILL is withheld from reads (a new `WITHHOLD_ACTIVITY(id, until)` simulator fault) while positions already reflect it;
   - P1 fails and sells of the asset are refused (`ACTIVITY_HISTORY_UNPROVEN`) on the first pass; the latch is set on the second;
   - *C* advances past its timestamp with later activities;
   - when the activity becomes visible, the overlap read finds it (ID ≤ *C*); it is classified and alerted `late_activity`, and P1 holds again;
   - with *Δ* shorter than the delay, the daily full re-read finds it instead.
4o. **Deduplication:** the same activity returned by the incremental, overlap and full reads produces one classification and one row.

**Expiry during submission (TP140-R5-03):**

4p. **The lease expires while `place_order` is in flight:**
   - `HOLD_CALL("place_order")` blocks the first holder's request while the clock passes the lease TTL;
   - a second holder acquires the reservation; its sell is refused `PRIOR_SUBMISSION_UNRESOLVED`;
   - the call is released, and the first holder records the outcome and starts nothing new;
   - exactly one exit exists at the broker.
4q. **Ambiguous submission, then a restart:**
   - `SUBMIT_AMBIGUOUS_ACCEPTED` plus lease expiry plus a process restart;
   - the intent stays `submission_unknown` with its full obligation;
   - a new runtime's exit is refused until recovery by `client_order_id` finds the order;
   - Σ submitted ≤ the bound, and no duplicate.
4r. **The first holder's late response after the second acquired:** its outcome is recorded once, and nothing is resubmitted. The second holder's *Ω* still counted it in full throughout.

**Concurrent protective exits (TP140-R4-02).** Each runs as concurrent asyncio tasks against one gateway, and again against two gateways on separate connections to the same database file. The fixture is 10 shares, with an earlier sell of 4 partially filled (2), its fills delayed, and settlement lagging:

4f. **Two protective exits at once:**
   - exactly one acquires the reservation; the other is skipped and retried next pass;
   - the first is sized to sellable: *B*ₗ = 10 − (4 − 0) = 6, and *B*ᵦ = *P* − 4. That gives 6 while positions lag (*P* = 10), and 4 once *P* reflects the 2 filled (*P* = 8). Sellable = min of the two;
   - after it, the second's *Ω* includes it, and its sellable is 0;
   - Σ submitted ≤ that bound (6 or 4), in both position-lag cases.
4g. **A reduced exit, then a competing one:**
   - the first requests 10 and is reduced to the bound (6 or 4, as in 4f), with `SELLABLE_BELOW_REQUESTED`;
   - the second, after delayed fills arrive but before settlement, gets 0;
   - after settlement of everything, sellable is again min(*P*, *L*) and never more than the true remainder.
4h. **Lease lost mid-authorization:**
   - the first's lease expires after it persists `risk_approved` but before the fence; the fence refuses submission;
   - the second, starting meanwhile, counts the first's approved quantity in full until it is marked rejected;
   - Σ submitted ≤ the bound.
4i. **No token:** a sell without `symbol_lock_owner_token` is refused (`EXECUTION_RESERVATION_REQUIRED`) before any broker read.
4j. **Seeded concurrency:**
   - 200 seeded trials of 2 to 5 concurrent protective exits, with random delays, partial fills, `POSITIONS_LAG` and lease expiries;
   - in every trial Σ submitted sell quantity ≤ the initial min bound, plus inventory settlement proved meanwhile, and the simulator's true long quantity never goes below 0.

**Oversell and quarantine:**

5. **Broker-side oversell:**
   - a fault-injected fill of 12 against 10: 10 closes, and *Q* = 2 becomes a liability;
   - one cash entry for the full fill; the latch is set; no short lot;
   - I1 to I3 hold.
6. **A trade outside the account:** an out-of-band sell to −3 latches `UNEXPLAINED_SHORT_POSITION`. No liability exists until the fill is recovered, then exactly one.
7. **Settlement recovery:** crashes after `project_lot` and after `project_cash` resume to one liability, one cash entry, one latch, one alert.
8. **The monitor:** no order of any kind on a negative or quarantined asset; the alert is once per day; other assets' stops fire.
9. **Valuation:**
   - marks move up, then down, then gap ±20 %;
   - exposure and equity follow the adverse rule of 3.3 per sign;
   - the broker identity holds to the cent.

**Fees (R3, TP140-03):**

10. **Proportional allocation:**
    - 8 closed and 2 excess on a −0.05 fee: −0.04 and −0.01;
    - 2 and 1 on −0.01: 0.00 and −0.01;
    - I4 exact.
11. **Signed conservation, through every case, asserting C1 after each step:**
    - a positive charge;
    - a reversal;
    - a re-post;
    - a correction between two partial resolutions, spread over the remaining quantity;
    - a correction after full resolution, giving one dated adjustment;
    - a cover fee posting the next evening;
    - a changed amount under the same fee ID → `BROKER_EVIDENCE_CHANGED`.

**Resolution (R1, R4, TP140-02, R8):**

12. **Partial covers:**
    - liabilities of 2 (older) and 3; a cover of 4 resolves 2, then 1 of the 3; a cover of 2 resolves the rest;
    - P&L per 3.7;
    - the hold stays until the end, and `reset-integrity` is refused until the fresh-reconciliation proof passes.
13. **The audit's worked example:** short 2 at $100, cover 2 at $90. Cash: +$200 and −$180, one entry each. Realized P&L +$20 before fees. Open quantity 0.
14. **Exactly once, in every recovery order:**
    - reconciliation before the command, then after it;
    - the command crashing mid-transaction and retried;
    - the command twice;
    - `replay_accounting` after the command;
    - each gives one execution row and one cash entry, and `missed_fills_detected` returns to 0 only through the evidence proof.
15. **No heuristic match:** a cover with the same symbol, quantity and price as an unrelated TradePulse buy within the time window is **not** matched to it.
16. **Surplus (R4):**
    - a cover of 5 against a liability of 2: the liability resolves, and a 3-share unmanaged long is created from the same execution;
    - one cash entry for all 5;
    - the hold stays;
    - a later verified sale of 3 resolves the surplus, and only then can the hold clear.
17. **Refusals:**
    - a wrong instrument, the wrong side, an activity before the record, a reused activity, an activity with an intent, a missing activity, a changed raw payload, a corporate action;
    - each is refused with nothing written.

**Legacy, schema and compatibility (TP140-04, R6, R7):**

18. **A legacy short lot:**
    - a fixture with an open short lot latches `LEGACY_SHORT_LOT`;
    - `adopt-legacy-lot` creates the liability and a conversion row; the lot and fill bytes are unchanged; I2 and I3 hold;
    - a lot with closures is refused;
    - an evidence database is refused;
    - a replay of the fixture is identical every time.
19. **Triggers:**
    - `UPDATE` and `DELETE` are refused on all ten tables;
    - a database missing one trigger is refused at startup;
    - an online backup and restore keeps every trigger.
20. **Checkpoint compatibility (R7):**
    - fixture databases with settlement events checkpointed **by today's main code** (`c10bb2fa`) at each existing stage, with and without an oversell;
    - each resumes under Rev.140 to the same ledgers as a fresh run, passing through `project_liability_hold`.
21. **Replay:** every preserved soak database, and a copy of the service database, replays to identical ledgers. The fixtures of tests 18 and 20 replay deterministically.
22. **Decimal:** every financial value in tests 1 to 21 is `Decimal`; the JSON paths reject floats.

## 8. Remaining open questions

**Decided by the operator (2026-10-26):**
- **R9. Approved:** fail-closed corporate-action classification, from Alpaca's documented activity types plus instrument-specific evidence. Any unrecognised non-trade activity naming the instrument refuses.
- **R10. Approved:** at most 3 attempts, then refusal on unresolved inconsistency. Agreement between reads is not proof of atomicity (3.1).
- **R11. Approved:** adopting unmanaged long inventory stays outside Rev.140. A resolved liability never turns its surplus into strategy inventory automatically.

- **R12. Approved (2026-10-26):** the simulator faults live beside Rev.135's in `tradepulse/broker/simulated.py`. They are `POSITIONS_LAG`, `STEP_BETWEEN`, `ACCEPT_OVERSELL`, `OVERFILL`, `WITHHOLD_ACTIVITY` and `HOLD_CALL`. Conditions:
  - they are reachable only through `SimulatedBroker` methods;
  - no `Settings` key, environment variable or CLI flag can enable them;
  - production broker construction (`build_broker`) never returns a `SimulatedBroker`. Tests prove each condition.

## 9. Consistency with A1

| Rev.140 element | A1 form |
|---|---|
| The fill cursor *C*, unresolved executions, S2 | Per account: *C* and the unresolved records gain `account_key`, and the reservation is per position key (A1 3). |
| The sellable bound and S1 | Per account: *L* from that account's lots, *P*, *O₁*, *O₂* and *A* from that account's broker, *Ω* keyed by `(account_key, broker_order_id)`. Read outside the risk decision window (A1 6.3). A1 6.4's freshness rule applies to the reads, but freshness is not treated as atomicity. |
| Activity IDs, `fill:cash:<id>`, I11 | The A1 canonical ID `<account_key>\|<venue>\|ExecutionEvent\|<broker id>` (A1 5.2). Cash keys gain the account (A1 3). |
| The cursor bootstrap and P1 (rev. 6) | Per account: one `fill_cursor_bootstrap` row per `account_key`, and P1 per position key. A1's migration may not create *C* for a new account by any route other than the bootstrap command. |
| S3 and `PRIOR_SUBMISSION_UNRESOLVED` (rev. 6) | Per position key; the submission identity stays the intent ID sent as `client_order_id` (A1 5.2). |
| The ten tables and their triggers | Gain `account_key`. A1's migration (7.3) must re-create every trigger after any table rebuild, and the 7.4 equivalence checks include them. |
| I2 | Per position key (account and instrument) |
| Quarantined records in risk | Counted in A1's consolidated exposure and equity by the sign rules of 3.3 (A1 6.7) |
| The latch | A1's per-account integrity cause; entries blocked globally (A1 6.6) |

## 10. Change log (rev. 3)

| Finding / decision | Change | Sections |
|---|---|---|
| **TP140-01** sellable double counting | Synchronized snapshot (orders, positions, orders); a per-order reservation table for every state; invariant S1; refusal on unstable, invalid or negative evidence; rev. 2's "approved minus settled fills", which double-counted delayed settlement, removed | 3.1, 4, tests 1–4 |
| **TP140-02** cover cash contract | Covers stored as `quarantine_executions` (not `Fill`, which requires an intent) and cash in the shared `fill:cash:<activity id>` namespace; an exactly-once table for every path; replay extended; worked example | 3.7, 4 (I1, I11), tests 13–14 |
| **TP140-03** signed fee conservation | Signed allocations; recognitions as records; equation C1 (*A* = *R* + *U*); recognition from the current unrecognised balance; dated adjustments after resolution; rev. 2's I6 replaced | 3.6, 4, test 11 |
| **TP140-04** legacy short lots | Detection and latch; `adopt-legacy-lot` with four preconditions; append-only conversion map; lots and fills never rewritten; partly closed lots and evidence databases refused | 3.9, test 18 |
| **TP140-05** corporate actions | Fail closed; seven minimum requirements for a future procedure | 3.10, test 17 |
| **R4** surplus | Unmanaged long inventory from the same execution; the hold stays until it is resolved | 3.7, 3.8, test 16 |
| **R5** | No automatic adjustment | 3.10 |
| **R6** | Triggers on all nine tables; startup verification; backup test | 3.11, test 19 |
| **R7** | Compatibility tests with events checkpointed by today's code | 5, test 20 |
| **R8** | Exact-ID matching only; the hold clears on a fresh evidence proof, never a flag | 3.5, 3.7, test 15 |
| Found while tracing | `pnl_evidence`'s closure conservation would have rejected every oversell; it is amended to include the liability | 1, 3.2 |
| Found while tracing | Heuristic fill matching could pair a cover with an unrelated buy; disabled for quarantined assets | 1, 3.5 |
| Valuation (audit section 4) | Exposure and equity marks defined separately by sign; gap stress test | 3.3, test 9 |
| **TP140-R3-01** (rev. 4) non-atomic reads | 3.1 replaced. Sellable is now the minimum of a local bound and a broker bound: the local one immune to position lag, the broker one counting each order at its full original quantity. An activity check covers external executions not yet in positions. Terminal orders stay counted until settled. Matching reads are no longer treated as proof. The residual (an external order after the last read) is stated, and detected by reconciliation. Protective exits are reduced, not refused. | 3.1, 4 (S1), 6, tests 1–4b |
| R9–R11 (rev. 4) | Decided as above | 8 |
| **TP140-R4-01** (rev. 5) cursor safety | A durable per-account fill cursor *C* that advances only with an outcome or an unresolved row for every activity (K1). Durable unresolved-execution records, at any age, block sells of that asset and `reset-integrity`. Outcomes only from exact-ID accounting evidence. Closes today's 24-hour `fill_lookback` ageing gap. | 3.1, 3.1a, 3.11, 4 (K1, I8), tests 4c–4e |
| **TP140-R5-01** (rev. 6) cursor bootstrap | *C* is created only by `reconcile bootstrap-fill-cursor`: full history read, an inception check, exact position and cash conservation (P1), and a classification for every FILL (`matched_fill`, `pre_database`, `legacy_reconciled` or unresolved) with legacy evidence cited, not rewritten. Any failure writes nothing, the runtime refuses to start, and the deployment rolls back. | 3.1a, 4 (P1), tests 4k–4m |
| **TP140-R5-02** (rev. 6) late activities | *C* is an optimization, not proof. An overlap read (*Δ* = 7 days, not a proven bound), a daily full re-read, deduplication by activity ID, and `late_activity` classification. Completeness rests on P1 every pass: sells refused on a mismatch, latch if it persists. *Δ* is checked against the A0 run's measured lag. | 3.1a, 4 (P1), tests 4n–4o |
| **TP140-R5-03** (rev. 6) expiry in flight | Obligations tied to the submission identity (S3), never to the lease. A gateway-level `PRIOR_SUBMISSION_UNRESOLVED` rule against duplicate exits. The original holder only records its outcome. | 3.1b, 4 (S3), tests 4p–4r |
| R12 (rev. 6) | Approved as test-only: no settings or CLI path; `build_broker` never returns the simulator | 8 |
| **TP140-R4-02** (rev. 5) concurrent exits | The reservation is mandatory for sells and verified before any read. A fixed order of operations, with the intent persisted before release. Invariant S2 (total order), across processes. | 3.1b, 4 (S2), tests 4f–4j |

## 11. Evidence required for implementation approval

1. The A0 verification run concluded, its criteria decided, and its report and database preserved with SHA-256. The operator has reviewed that evidence.
2. Independent review of rev. 6's sections 3.1 to 3.1b.
3. **Read-only evidence before Task 4,** each kept in `~/tradepulse-evidence/` with a manifest:
   - the cash identity of 3.1a step 4 shown exactly on the paper account;
   - the A0 run's maximum observed activity lag, measured, with *Δ* checked against it.
4. Explicit operator authorization to implement.
5. Then, before merge: the 40 tests, the full suite, mypy and ruff green; test 21's replay; the operator's review of the Rev.140 zip.
