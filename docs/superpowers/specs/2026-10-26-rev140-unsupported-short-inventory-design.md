# Rev.140: unsupported short inventory — design specification

**Status:** revision 2, for independent review. **Design only.**
- This authorization covers design updates, nothing more: no code, schema, test, merge, push or broker order.
- Implementation starts only after:
  - the A0 verification run (Mon 2026-10-26 09:00 ET to Tue 2026-10-27 16:30 ET) has concluded;
  - its report and database are preserved with SHA-256;
  - the accounting and recovery invariants here have been independently reviewed;
  - the operator approves.
- The existing Alpaca service, prove-edge records and verification generations are not touched.

**Revision history:**
- **rev. 1** (2026-10-26): first draft.
- **rev. 2** (2026-10-26):
  - records the operator's decisions on R1 to R3;
  - makes liabilities and their resolutions immutable, append-only records (3.2, 3.7);
  - adds fee allocation with exact remainder and delayed corrections (3.6);
  - adds the accounting invariants (section 4) and recovery behaviour (section 5);
  - expands the tests to 16 (section 7), including legacy short-lot replay;
  - adds R4 to R8.

**Origin:**
- the operator's decisions C10, D6 and R1 to R3 (2026-10-25 and 2026-10-26);
- the A1 specification (`phase-a`, sections 6.9 and 12);
- U4: shorts are outside scope.

**Scope:** a narrowly scoped correction to today's single-account Alpaca runtime, on main. A1 later adds `account_key` to its records.

## 1. What happens today (traced in the code)

| Path | Behaviour | File |
|---|---|---|
| Who sends sells | Only the position monitor. The scanner is buy-only. | `monitor/coordinator.py`, `scanner/coordinator.py` |
| Overlapping internal sells | **Already prevented on the monitor path.** The monitor holds the asset's execution reservation and skips the asset if `has_in_flight_intent` finds any intent in `risk_approved`, `submitted`, `accepted`, `partially_filled` or `submission_unknown`. | `monitor/coordinator.py:492-496`, `execution/idempotency.py:14,45-64` |
| Gateway sell check | `requested ≤ broker held quantity` (`INSUFFICIENT_POSITION_TO_SELL`). **Not net** of other sell intents or broker open orders, so the gateway relies on its caller's discipline. Crypto protective exits re-read positions immediately before submission. | `risk/engine.py:244-248`, `execution/gateway.py:263-267,530-543` |
| An oversell fill | `plan_signed_lot_fill` closes the long lots and **opens a short lot** with the excess. `_project_holding` sums signed quantities, giving a **negative holding**. Cash receives the full proceeds, which is correct. No hold. | `settlement/lots.py:38-101`, `settlement/engine.py:66-119,280-323` |
| A negative holding | The monitor sets `exit_side = BUY`, and the gateway classifies a buy against a negative position as a protective exit. **The short is covered automatically.** | `monitor/coordinator.py:498-499`, `execution/gateway.py:266-267` |
| A trade outside TradePulse | Reconciliation reports an activity with no local fill as a missed fill and latches `FINANCIAL_INTEGRITY_BLOCKED`. A broker position without a holding is alerted as unmanaged. | `reconciliation/coordinator.py:440-468`, `monitor/coordinator.py:446-452` |
| Fee allocation | An order-level cash fee is allocated across its FIFO closures in proportion to proceeds. Each share is rounded down to the fee's precision; the last share receives the exact remainder. | `reconciliation/cash_fees.py:1-45` |
| Equity identity | `valuation.py` includes `short_market_value` in the broker identity, which is already correct for a negative position | `valuation.py:210-218` |

**Evidence, and its limit.** No short lot exists in the six preserved soak databases or in the service database `tradepulse.db` (all opened read-only). That means no *known* historical short lot needs migrating in the inspected databases. It does **not** remove the need to test replay, migration and recovery with a short lot present: tests 13 and 14 do so with synthetic fixtures.

**The gaps:**
1. The gateway does not itself net pending sells; a future caller could bypass the monitor's guard.
2. The gateway does not consult broker open orders, including orders placed outside TradePulse.
3. An oversell becomes an ordinary short lot and a negative holding.
4. The monitor would automatically cover it.
5. Nothing raises an integrity hold for it.

## 2. Requirements (operator decisions C10, D6, R1 to R3)

1. TradePulse cannot knowingly submit an oversell. Prevention is its own, from:
   - verified inventory;
   - in-flight reservations;
   - broker open orders.
   **A broker rejection is never the safety mechanism (R2).**
2. An actual broker oversell is preserved whole: the fill, cash proceeds, execution identifiers and fees.
3. Only verified long inventory is closed, by the existing lot rules.
4. The excess is a quarantined short-exposure liability, never a tradable lot and never a strategy-managed position.
5. It is included in equity, mark-to-market valuation, reconciliation and conservative risk.
6. Realized P&L on the excess is unrecognized until it is economically closed, or authoritative broker evidence establishes its disposition.
7. No automatic trading against it.
8. An integrity hold applies, kept until the whole exposure is resolved *and* reconciliation proves every related record agrees.
9. **Partial resolution (R1):**
   - from verified broker covering executions, oldest first within the exact account and instrument;
   - the original oversell is preserved, with an immutable link between each liability and each covering activity;
   - P&L is realized only on the quantity covered.
10. **Fees (R3):**
    - each fill-level fee is allocated between the long closure and the excess in proportion;
    - in `Decimal`, with deterministic rounding, reconciling exactly to the original fee;
    - delayed corrections are processed without double counting and without changing immutable execution facts.
11. Never:
    - discard a real execution;
    - fabricate inventory;
    - convert a negative quantity to long;
    - erase it;
    - turn a cover into an ordinary long trade.

## 3. Design

### 3.1 Prevention in the gateway

A sell's **sellable quantity** is computed inside the gateway, under the asset's execution reservation:

  sellable = broker held quantity − Σ unfilled quantity of this asset's other SELL intents in an in-flight or `submission_unknown` status − Σ unfilled quantity of broker open SELL orders for this asset with no matching intent

- **Sources:**
  - `get_positions()`, already fetched;
  - `get_open_orders()`, one more broker call, made before the decision window (as A1 6.3 requires);
  - the trade-intent store.
- **Matching:** a broker open order is matched to an intent by `client_order_id`, which equals the intent ID. An unmatched open order is out-of-band: it is subtracted, and it is alerted.
- **Refusals:**
  - a sell above sellable is refused: `INSUFFICIENT_SELLABLE_QUANTITY (held h, pending p, requested r)`;
  - a sell when held ≤ 0, a buy classified as protective against a negative position, or any order on an asset with an open liability, is refused: `UNSUPPORTED_SHORT_POSITION`. This removes the gateway's cover path.
- **Failure:** if `get_open_orders()` fails, the sell is refused (`BROKER_OPEN_ORDERS_UNAVAILABLE`), as today for unavailable positions. The monitor retries next pass.
- **Partial fills:** an intent's unfilled quantity is its approved quantity minus settled fills, so a partially filled sell reserves only what is left.
- **No broker dependency (R2):** the check runs with a simulator that accepts any oversell (test 2).
  - Whether Alpaca rejects a long-to-short sell is not tested.
  - Any later experiment needs separate operator authorization, an isolated paper account, and isolation from the active verification generation.

### 3.2 Settlement of an oversell

`plan_signed_lot_fill` is unchanged; it still reports the excess as `opening_quantity` in the opposite direction. In `_project_lot`:
- **A SELL whose plan opens a `short` quantity creates no lot.** It creates one `ShortExposureLiability`, with `create_once` unique on `fill_id`. Its opening facts are **immutable** after creation:
  - `liability_id`, `fill_id`, `broker_order_id`, `trade_intent_id`, the asset;
  - `quantity` (the excess), `sale_price` (the fill price), the multiplier;
  - `proceeds` = quantity × price × multiplier;
  - `opened_at`.
- **Status is derived, never stored.** The open quantity is the original quantity minus Σ resolutions (3.7); it is `quarantined` while above 0 and `resolved` at 0.
- **The closing part** settles exactly as today: lot closures, realized P&L and attribution.
- **Cash:** `project_cash` is unchanged and books the full fill once.
- **Holdings:** `_project_holding` is unchanged. Open lots are long only, so a holding fully closed by the oversell is deleted.
- **The hold.** A new final settlement stage, `project_liability_hold`, runs after `verify_integrity`:
  - it calls `latch_financial_integrity_block(reason="UNSUPPORTED_SHORT_POSITION: <liability_id>")`, which is idempotent;
  - it writes an integrity hold on the broker order;
  - it sends a critical alert once per liability.
  The settlement event completes, so the ledger is whole and a replay is a no-op.
- **A BUY whose plan would close `short` lots** (a legacy short lot): it raises `INTEGRITY_VIOLATION: UNSUPPORTED_SHORT_LOT`. The event is `INTEGRITY_BLOCKED`, and nothing trades against it (test 13).

### 3.3 Valuation, exposure and risk

- **Local position value:** each liability's open quantity × current mark is *subtracted* from local holdings value. Local equity = cash + long holdings value − open liabilities.
- **Unrealized P&L on the open excess** = open quantity × (sale price − current mark) × multiplier. It is shown in the dashboard and in the equity snapshot's `short_exposure_liabilities` block.
- **Risk:** exposure counts each open liability at its absolute value, at the higher of the last confirmed and the current mark. Entries are refused anyway while the latch holds.
- **Valuation check:** the broker's `short_market_value` must equal −Σ open liability value within the broker's cent rounding, which `valuation.py` already reproduces.

### 3.4 The monitor

- A negative broker position, or an asset with an open liability, gets **no order of any kind:** no stop, ladder, time stop or forced close.
- Each pass alerts `UNSUPPORTED_SHORT_POSITION` through `_alert_once`, once per liability per day.
- Other assets continue as today.

### 3.5 Reconciliation

- **Quantity:**
  - the broker's position quantity must equal Σ open long lots − Σ open liability quantity, exactly (invariant I2);
  - a broker position < 0 with no matching liability, or a non-zero `short_market_value` without one, latches `FINANCIAL_INTEGRITY_BLOCKED` (`UNEXPLAINED_SHORT_POSITION`).
- **No liability is fabricated** from a position read. One is created only from the authentic execution, when settlement or late-fill recovery processes it.
- **A trade outside TradePulse** is today's missed-fill path. It already latches; Rev.140 adds only the position-level check above.

### 3.6 Fees (R3)

- **The rule is today's `allocate_cash_fees`, extended.** For each posted fee linked to the oversell's order:
  - the allocation targets are the order's FIFO closure attributions plus the liability;
  - shares are proportional to proceeds. The oversell is one fill at one price, so this is proportional to quantity: for 8 closed and 2 excess, 80 % and 20 %;
  - each share is rounded down to the fee's own precision (`ROUND_DOWN`, quantum taken from the posted amount);
  - **the liability is ordered last, so it receives the exact remainder.** Σ allocations = the posted fee exactly (I4).
- **Crypto in-asset fees** (`CFEE`) follow the existing asset-fee FIFO policy (`reconciliation/asset_fees.py`):
  - asset units fall on held long inventory first;
  - an in-asset fee that would draw on the excess raises `INTEGRITY_VIOLATION: ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY`, as today, and holds.
- **Allocation records are append-only:** `liability_fee_allocations(fee_canonical_id, liability_id, amount)`, unique on `(fee_canonical_id, liability_id)`.
  - A replay of the same fee is a no-op.
  - A different amount for the same fee ID is `BROKER_EVIDENCE_CHANGED`, never an overwrite.
- **Delayed corrections:**
  - a later broker fee activity that corrects or reverses an earlier one has its own canonical ID;
  - it gets its own allocation rows by the same rule: a reversal allocates the negated amounts of the original's rows, and a correction allocates its own amount;
  - the original rows are never edited, and execution facts never change;
  - the liability's fee total is Σ of its rows.
- **Recognition:** the excess's fee share is posted to the ledger like any fee, but enters realized P&L only at resolution (3.7), in proportion to the quantity resolved.

### 3.7 Resolution: controlled reconciliation only (R1)

Only the operator command `tradepulse liability resolve --activity <broker activity id> --reason "<text>"` resolves a liability.

**1. Verification. It refuses unless every check passes:**
- the activity is a broker `FILL` in the account's activities, read fresh;
- it is a BUY of the exact instrument;
- it executed after the oldest open liability opened;
- it is not already used by any resolution;
- it has no TradePulse intent. A cover is never a TradePulse order (3.1).

**2. Matching, oldest first.** The cover quantity is applied to open liabilities of that instrument in `opened_at` order, then `liability_id`. A cover smaller than the open quantity resolves partially. A cover larger than all open liabilities is refused (R4).

**3. One SQLite transaction writes:**
- **The cover fill**, imported as an accounted fill of kind `liability_cover`. It books cash once, through the existing cash-ledger path, and **creates no lot and no holding**, so a cover never becomes an ordinary long trade.
  - It is marked recovered for the missed-fill gate, so `reset-integrity`'s `missed_fills_detected` check counts it as accounted (R8).
- **One `liability_resolutions` row per liability touched:** `liability_id`, `activity_id`, quantity, cover price and realized P&L. Unique on `(liability_id, activity_id)`.
- **Realized P&L** for the covered quantity *q* of a liability with original quantity *Q*:

    q × (sale_price − cover_price) × multiplier − (q / *Q*) × Σ opening-fee allocations − the cover fill's own allocated fees

  - Each fee term is computed with the same `ROUND_DOWN` + exact-remainder rule. The final resolution that brings the open quantity to 0 receives the remainder, so Σ recognised fees equals Σ allocated fees exactly.
  - Other adjustments (a corporate action on the instrument) are not handled by the command: the resolution is refused and the hold stays (R5).
- **An audit event.**

**4. Releasing the hold.** The hold is not released by the command. `reset-integrity` is refused while:
- any liability has open quantity > 0, or
- any invariant in section 4 fails on a fresh reconciliation: cash, fee conservation, quantity identity, and the valuation check.

Rev.140 sends no cover order. The operator closes the position at the broker, and the command accounts for it.

### 3.8 Schema

Three new payload tables, created with `CREATE TABLE IF NOT EXISTS`. They are additive and need no rebuild:

| Table | Unique | Mutability |
|---|---|---|
| `short_exposure_liabilities` | `fill_id` | insert-only |
| `liability_resolutions` | `(liability_id, activity_id)` | insert-only |
| `liability_fee_allocations` | `(fee_canonical_id, liability_id)` | insert-only |

- SQLite triggers reject `UPDATE` and `DELETE` on all three, as A1 does for its migration map (R6).
- A1 adds `account_key` to each.

## 4. Accounting invariants

Each one is checked by reconciliation on every pass, and by the tests.

| # | Invariant | A failure means |
|---|---|---|
| I1 | Every broker fill, including an oversell and a cover, has exactly one cash-ledger effect, at its full authentic amount | latch |
| I2 | Per asset: broker quantity = Σ open long lot quantity − Σ liability open quantity, exactly | latch |
| I3 | Per oversell fill: fill quantity = Σ its lot closures + its liability's original quantity | `INTEGRITY_VIOLATION` |
| I4 | Per posted fee: Σ attribution allocations + Σ liability allocations = posted amount, exactly | `INTEGRITY_VIOLATION` |
| I5 | Per liability: Σ resolution quantity ≤ original quantity, and realized P&L is recognised once per `(liability, activity)` | `INTEGRITY_VIOLATION` |
| I6 | Per liability: Σ fees recognised through resolutions ≤ Σ allocated; equal when fully resolved | `INTEGRITY_VIOLATION` |
| I7 | No lot, holding or strategy-managed record originates from an oversell's excess or a cover fill | `INTEGRITY_VIOLATION` |
| I8 | Any open liability ⇒ the session is `FINANCIAL_INTEGRITY_BLOCKED` | re-latch at startup and every reconciliation |
| I9 | Liabilities, resolutions and allocations are never updated or deleted | trigger refusal |
| I10 | Every financial value is `Decimal`; no float enters, including from JSON (parsed with `parse_float=Decimal`) | test failure |

## 5. Recovery behaviour

| Crash point | On restart |
|---|---|
| After `project_lot` creates the liability, before `project_cash` | Settlement resumes at `project_cash`. `create_once(fill_id)` makes the liability a no-op; cash is booked once (I1). |
| After `project_cash`, before `project_liability_hold` | The stage re-runs: one latch, one hold, one alert (idempotent). I8 also re-latches at startup. |
| A posted fee arriving during an outage | Allocated on the next reconciliation; the unique key makes a replay a no-op |
| A fee correction arriving after a resolution | Allocated by 3.6. If the liability is already fully resolved, the delta is recognised as a separate, dated P&L adjustment row linked to the correction, never by editing the resolution. |
| During `liability resolve` | One transaction: either the fill, the resolutions, the P&L and the audit event all exist, or none do. A retry with the same activity is refused as already used, or completes if nothing committed. |
| A restart with an open liability | The monitor skips the asset (3.4); the gateway refuses it (3.1); I8 keeps the latch. |
| Restore from backup | The new tables are in the same database file and are covered by the existing backup. A restore to a point before an oversell replays it from broker activities through settlement. |
| An upgrade with settlement events in progress | `project_liability_hold` is appended after every existing stage, so an event checkpointed at an earlier stage resumes through it unchanged (R7). |

## 6. Behaviour changes in the live runtime

| Change | When it is visible |
|---|---|
| Sells are netted against pending sells and broker open orders | Only when another sell is pending. Today's monitor guard already prevents this, so normally never. |
| One more broker call (`get_open_orders`) per sell | Every exit; one request, within the rate limit |
| `UNSUPPORTED_SHORT_POSITION` refusals; no automatic cover | Only with a negative position: none in the inspected databases |
| An oversell becomes a liability and a latch, not a short lot | Only on an oversell: none in the inspected databases |
| Three new tables and an equity-snapshot block | Always present, normally empty |
| A new final settlement stage | Every settlement; a no-op without a liability |

Replaying every preserved soak database must give identical ledgers and decisions.

## 7. Tests (deterministic, on `SimulatedBroker` with its clock; no network)

**Prevention:**
1. **Partial fills:**
   - a sell of 10 against 10 fills 4, then 6;
   - in between, a second sell of any size is refused `INSUFFICIENT_SELLABLE_QUANTITY`;
   - afterwards, sellable is 0.
2. **Overlapping sells, no broker safety net:**
   - the simulator is configured to accept oversells;
   - two exits for the same asset race, called directly against the gateway (monitor guard bypassed); exactly one is approved;
   - a broker open sell with no intent reduces sellable, and is alerted;
   - no order the gateway submits can exceed held quantity, over a seeded randomized sequence of 1,000 sell requests with a fixed seed.

**Oversell and liability:**

3. **Broker-side oversell:**
   - a fault-injected fill of 12 against a held 10;
   - 10 closes the lots with realized P&L, and 2 becomes a liability; cash has the full proceeds;
   - the session is `FINANCIAL_INTEGRITY_BLOCKED`, and no lot is short;
   - I1 to I3 hold.
4. **A trade outside the account:**
   - an out-of-band sell takes the account to −3;
   - reconciliation latches `UNEXPLAINED_SHORT_POSITION`;
   - no liability exists until the fill is recovered, then exactly one.
5. **Settlement recovery:**
   - crashes after `project_lot`, and after `project_cash`;
   - on resume: one liability, one cash entry, one latch, one alert.
6. **The monitor:** with a negative position, no order of any kind across several passes; the alert is once per day; another asset's stop still fires.
7. **Liability valuation:**
   - the mark moves up, then down;
   - local equity, unrealized P&L and exposure follow;
   - the broker identity with `short_market_value` holds to the cent.

**Fees (R3):**

8. **Proportional allocation:**
   - 8 closed and 2 excess, fee 0.05: 0.04 and 0.01;
   - 2 closed and 1 excess, fee 0.01: 0.00 to the closure, 0.01 to the liability (the remainder);
   - Σ = posted, exactly, in every case (I4).
9. **Delayed fee:** the fee posts the next evening, after a restart; it is allocated once, and a replay is a no-op.
10. **Fee correction:**
    - a reversal and a re-post of a corrected amount each add rows, and the originals are unchanged;
    - after full resolution, a late correction creates one dated adjustment row;
    - a changed amount under the same fee ID is `BROKER_EVIDENCE_CHANGED`.

**Resolution (R1):**

11. **Partial covers:**
    - liabilities of 2 (older) and 3; a cover of 4 resolves 2, then 1 of the 3;
    - a second cover of 2 resolves the rest;
    - realized P&L per the 3.7 formula, recognised once;
    - the hold stays until the last;
    - `reset-integrity` is refused until every invariant holds on a fresh reconciliation.
12. **Refusals:**
    - a wrong instrument, a SELL, an activity before the liability, a reused activity, an activity with a TradePulse intent, a cover larger than the open total, a missing activity;
    - each refused with nothing written.
13. **Legacy short lot:**
    - a fixture database with a pre-existing short lot;
    - a BUY settling against it is `INTEGRITY_BLOCKED` (`UNSUPPORTED_SHORT_LOT`);
    - no automatic cover; I8 latches.
14. **Replay and migration:**
    - every preserved soak database, and a copy of the service database, replays to identical ledgers with the new stage and tables;
    - the fixture of test 13 replays to the same hold every time.
15. **Immutability:** `UPDATE` and `DELETE` on the three tables are refused by the triggers.
16. **Decimal:** every financial value produced in tests 1 to 15 is `Decimal`, and the JSON paths reject floats.

## 8. Open questions for review

- **R4. A cover larger than the open liabilities.** The surplus would be a long position bought outside TradePulse.
  - **Proposed:** refuse the command, and leave the hold; the operator reconciles the surplus as an unmanaged position first.
  - Alternative: resolve the liabilities and record the surplus as an unmanaged long, not strategy-managed.
- **R5. Corporate actions on an instrument with an open liability** (split, symbol change). **Proposed:** out of Rev.140's scope; the resolution is refused and the hold stays until a reviewed procedure exists.
- **R6. Append-only enforcement by SQLite triggers.** No payload table uses triggers today; the A1 migration map is the precedent. Confirm that triggers are acceptable in the v1 schema.
- **R7. The settlement stage list gains a final stage.** The design relies on an event checkpointed at an earlier stage resuming through every later stage (`settlement/stages.py`). Implementation must prove this against an event checkpointed by today's code.
- **R8. The missed-fill gate.** An out-of-band cover is first reported as a missed fill and latches. The design marks it recovered when `liability resolve` imports it. The exact interaction with `reset-integrity`'s `missed_fills_detected` count must be shown in review against `reconciliation/coordinator.py:440-468`.

## 9. Evidence required for approval

1. The A0 verification run concluded, its criteria decided, and its report and database preserved with SHA-256.
2. Independent review of sections 4 and 5, with R4 to R8 decided.
3. The 16 tests, the full suite, mypy and ruff green.
4. Every preserved soak database, and a copy of the service database, replays to identical ledgers (test 14).
5. The operator's review of the Rev.140 zip before any merge.
