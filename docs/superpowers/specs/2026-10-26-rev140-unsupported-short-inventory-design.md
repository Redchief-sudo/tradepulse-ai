# Rev.140: unsupported short inventory — design specification

**Status:** revision 3, for independent review. **Design only.**
- This authorization covers design updates, nothing more: no code, schema, test, merge, push or broker order.
- Implementation starts only after:
  - the A0 verification run (Mon 2026-10-26 09:00 ET to Tue 2026-10-27 16:30 ET) has concluded;
  - its report and database are preserved with SHA-256;
  - its evidence has been reviewed;
  - the operator gives explicit authorization.
- Rev.139, its verification evidence and every scheduled Alpaca run are not touched.

**Revision history:**
- **rev. 1** (2026-10-26): first draft.
- **rev. 2** (2026-10-26): R1 to R3 decisions, immutable records, invariants, recovery, 16 tests.
- **rev. 3** (2026-10-26): the institutional audit's TP140-01 to TP140-05, and the operator's R4 to R8. Change log in section 10.

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

### 3.1 Prevention: sellable inventory (TP140-01)

**Pending sells are counted per order, never per fill or per settlement.**
- The sellable quantity for an asset is computed from **one synchronized broker snapshot**, plus the intent store read inside the asset's execution reservation.
- Each pending sell is counted exactly once, at its broker-reported unfilled remainder.

**The synchronized snapshot.** Reads in this order:
1. `get_open_orders()` (*O₁*);
2. `get_positions()` (*P*);
3. `get_open_orders()` (*O₂*).

The snapshot is valid only if, for every sell order of the asset, *O₁* and *O₂* agree on status and `filled_qty`.
- **Why this order:** a fill between the reads lowers *P* while the order still shows its old remainder. That under-states sellable, the safe direction.
- **Two consistent order reads** prove no fill happened between them, so *P* and *O₂* describe the same inventory.
- **Retries:** if the reads disagree, the snapshot is retaken up to 3 times. After that the sell is refused, `SELLABLE_SNAPSHOT_UNSTABLE`.

**The per-order reservation.** The *pending set* is built **keyed by broker order ID**, so each order appears once, from the union of:
- the asset's SELL intents in `risk_approved`, `submitted`, `accepted`, `partially_filled` or `submission_unknown`;
- the asset's SELL orders in *O₂*.

An intent is linked to its broker order by `client_order_id` (= intent ID). Each member's reservation *r*:

| Order state | *r* | Why |
|---|---|---|
| Intent `risk_approved`, not yet submitted | approved quantity | not yet at the broker, so not in *P* |
| Open at the broker (in *O₂*), with or without an intent | `qty − filled_qty` from *O₂* | the filled part is already out of *P*; the remainder is not yet |
| Intent in-flight but absent from *O₂* | resolved by `get_order(broker_order_id)` within the snapshot. Terminal (`filled`, `canceled`, `expired`, `rejected`): *r* = 0, its fills already in *P*. Lookup fails or is non-terminal: *r* = approved − broker `filled_qty` if known, otherwise the approved quantity. | never released on missing evidence |
| Intent `submission_unknown` | the approved quantity, until recovery proves the outcome | it may exist at the broker in any state |
| Broker open order with no intent (out-of-band) | `qty − filled_qty`; also alerted | it can consume inventory |

- **The rule:** sellable = *P*.qty − Σ *r*.
- **The request:** a sell is approved for at most sellable. Above that, `INSUFFICIENT_SELLABLE_QUANTITY (held, pending by order, requested)`.

**Invariant S1 (counted once).** Every pending sell order contributes exactly one *r*, keyed by broker order ID, or by intent ID before submission. *r* is derived only from one snapshot. Settlement status and local fills **never** enter *r*. A partial fill is thus neither double-counted (it is already out of *P*, and *r* excludes it) nor released (its remainder stays in *r* until the broker reports a terminal state).

**Refusals.** Each one refuses the sell; none releases anything:
- `BROKER_OPEN_ORDERS_UNAVAILABLE` or `BROKER_POSITIONS_UNAVAILABLE`;
- `SELLABLE_SNAPSHOT_UNSTABLE`;
- `filled_qty > qty` on any order (`BROKER_ORDER_EVIDENCE_INVALID`);
- sellable < 0 (`SELLABLE_NEGATIVE`). This also alerts critical: an oversell is in flight.
- `UNSUPPORTED_SHORT_POSITION`, which covers:
  - a sell when *P*.qty ≤ 0;
  - a buy classified as protective against a negative position;
  - any order on an asset with open quarantined inventory (3.2, 3.8).

This removes the gateway's cover path.

**Window.** The snapshot is taken before A1's local decision window (A1 6.3). It runs inside the asset's execution reservation, so no other TradePulse order on the asset can begin between the snapshot and submission. Out-of-band orders placed in that interval are caught by reconciliation (3.5), not prevented.

**No broker dependency (R2).** Test 2 runs against a simulator that accepts every oversell. Whether Alpaca rejects one is not tested; a later experiment needs separate operator authorization, an isolated paper account and isolation from the active verification generation.

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

**Immutability (R6).**
- SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers reject changes on all seven tables.
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
| I8 | Any open quarantined record or legacy short lot ⇒ `FINANCIAL_INTEGRITY_BLOCKED` | re-latch at startup and on every pass |
| I9 | The seven tables are never updated or deleted, and their triggers exist | refusal / `SCHEMA_TRIGGER_MISSING` |
| I10 | Every financial value is `Decimal`; JSON is parsed with `parse_float=Decimal` | test failure |
| I11 | Each broker activity ID maps to at most one of: a `Fill`, a `quarantine_executions` row | `INTEGRITY_VIOLATION` |
| S1 | Each pending sell order contributes exactly one reservation, derived from one synchronized snapshot (3.1) | refusal |

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
| Sellable quantity from a synchronized snapshot, per order | Every sell. Normally equal to today's held quantity, because the monitor allows one exit at a time. |
| Two extra broker calls per sell (`get_open_orders` twice) | Every exit; within the rate limit |
| `UNSUPPORTED_SHORT_POSITION` refusals; no automatic cover | Only with negative or quarantined inventory: none in the inspected databases |
| An oversell becomes a liability and a latch | Only on an oversell: none in the inspected databases |
| The closure conservation check includes the liability | Every settlement; identical without an oversell |
| Heuristic fill matching disabled for quarantined assets and covers | Only with quarantined records |
| Seven new tables with triggers; trigger verification at startup | Always; normally empty |
| A new final settlement stage | Every settlement; a no-op without a liability |

Replaying every preserved soak database must give identical ledgers and decisions.

## 7. Tests (deterministic, on `SimulatedBroker` with its clock; no network)

**Prevention (TP140-01):**
1. **Partial fills:**
   - a sell of 10 against 10 fills 4. The snapshot shows *P* = 6, and the order has `filled_qty` 4, so *r* = 6 and sellable = 0;
   - **local settlement of the 4 is delayed:** sellable is still 0, neither 6 (released) nor −4 (double-counted);
   - the remainder fills, and sellable stays 0.
2. **Overlapping sells, no broker safety net:**
   - the simulator accepts oversells;
   - two exits race, called directly against the gateway; exactly one is approved;
   - a seeded randomized sequence of 1,000 sell requests, partial fills, cancels and out-of-band orders (fixed seed) never yields a submitted sell above sellable.
3. **Order states:**
   - a canceled partial gives *r* = 0;
   - an expired order gives 0;
   - `submission_unknown` reserves the full approved quantity;
   - an in-flight intent absent from the open orders resolves through `get_order`; if that lookup fails, it reserves the approved quantity;
   - an out-of-band open order reduces sellable and alerts;
   - `filled_qty > qty` is refused.
4. **An unstable snapshot:** a fill injected between *O₁* and *O₂*, three times, gives `SELLABLE_SNAPSHOT_UNSTABLE`. Injected once, the retry succeeds with the post-fill figures.

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
    - `UPDATE` and `DELETE` are refused on all seven tables;
    - a database missing one trigger is refused at startup;
    - an online backup and restore keeps every trigger.
20. **Checkpoint compatibility (R7):**
    - fixture databases with settlement events checkpointed **by today's main code** (`c10bb2fa`) at each existing stage, with and without an oversell;
    - each resumes under Rev.140 to the same ledgers as a fresh run, passing through `project_liability_hold`.
21. **Replay:** every preserved soak database, and a copy of the service database, replays to identical ledgers. The fixtures of tests 18 and 20 replay deterministically.
22. **Decimal:** every financial value in tests 1 to 21 is `Decimal`; the JSON paths reject floats.

## 8. Remaining open questions

- **R9. The list of Alpaca corporate-action activity types** that trigger 3.10's refusal. **Proposed:** taken from Alpaca's activity-type documentation at implementation, with any unrecognised non-trade activity type naming the instrument also refusing (fail closed).
- **R10. Retries for an unstable snapshot.** 3 attempts is a proposal; each costs two broker calls. Confirm.
- **R11. Adoption of unmanaged long inventory** into strategy management. **Proposed:** out of scope for Rev.140; it is resolved only by a verified sale. Confirm.

## 9. Consistency with A1

| Rev.140 element | A1 form |
|---|---|
| Sellable snapshot and S1 | Per account. The snapshot is one of A1 6.4's complete snapshots; S1 keys orders by `(account_key, broker_order_id)`. Taken outside the decision window (A1 6.3). |
| Activity IDs, `fill:cash:<id>`, I11 | The A1 canonical ID `<account_key>\|<venue>\|ExecutionEvent\|<broker id>` (A1 5.2). Cash keys gain the account (A1 3). |
| The seven tables and their triggers | Gain `account_key`. A1's migration (7.3) must re-create every trigger after any table rebuild, and the 7.4 equivalence checks include them. |
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
| **R6** | Triggers on all seven tables; startup verification; backup test | 3.11, test 19 |
| **R7** | Compatibility tests with events checkpointed by today's code | 5, test 20 |
| **R8** | Exact-ID matching only; the hold clears on a fresh evidence proof, never a flag | 3.5, 3.7, test 15 |
| Found while tracing | `pnl_evidence`'s closure conservation would have rejected every oversell; it is amended to include the liability | 1, 3.2 |
| Found while tracing | Heuristic fill matching could pair a cover with an unrelated buy; disabled for quarantined assets | 1, 3.5 |
| Valuation (audit section 4) | Exposure and equity marks defined separately by sign; gap stress test | 3.3, test 9 |

## 11. Evidence required for implementation approval

1. The A0 verification run concluded, its criteria decided, and its report and database preserved with SHA-256. The operator has reviewed that evidence.
2. Independent review of rev. 3, sections 3.1, 3.6, 3.7 and 4, with R9 to R11 decided.
3. Explicit operator authorization to implement.
4. Then, before merge: the 22 tests, the full suite, mypy and ruff green; test 21's replay; the operator's review of the Rev.140 zip.
