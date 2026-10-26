# Rev.140: unsupported short inventory — design specification

**Status:** revision 1, draft for operator and external review.
- It is a design only: no code, schema or test change is made.
- Implementation starts only after:
  - the A0 verification run (Mon 2026-10-26 09:00 ET to Tue 2026-10-27 16:30 ET) has completed;
  - its report and database are preserved with SHA-256;
  - this document is approved.
- It does not alter or interrupt an active prove-edge run without the operator's explicit authorization.

**Origin:**
- the operator's decisions C10 and D6 (2026-10-25), recorded in the A1 specification (`docs/superpowers/specs/2026-10-24-a1-per-account-accounting-design.md` on `phase-a`, sections 6.9 and 12.4);
- U4: shorts are outside scope.

**Scope:** a narrowly scoped correction to today's single-account Alpaca runtime, on main. A1 later makes its records per account.

## 1. What happens today (traced in the code)

| Path | Behaviour | File |
|---|---|---|
| Who sends sells | Only the position monitor. The scanner is buy-only. | `monitor/coordinator.py`, `scanner/coordinator.py` |
| Overlapping internal sells | **Already prevented on the monitor path.** Before an exit, the monitor holds the asset's execution reservation and skips the asset if `has_in_flight_intent` finds any intent in `risk_approved`, `submitted`, `accepted`, `partially_filled` or `submission_unknown`. | `monitor/coordinator.py:492-496`, `execution/idempotency.py:14,45-64` |
| Gateway sell check | `requested ≤ broker held quantity` (`INSUFFICIENT_POSITION_TO_SELL`). **Not net** of other sell intents or broker open orders, so the gateway relies on its caller's discipline. Crypto protective exits re-read positions immediately before submission. | `risk/engine.py:244-248`, `execution/gateway.py:263-267,530-543` |
| An oversell fill | `plan_signed_lot_fill` closes the long lots and **opens a short lot** with the excess. `_project_holding` sums signed quantities, giving a **negative holding**. Cash receives the full proceeds, which is correct. No hold. | `settlement/lots.py:38-101`, `settlement/engine.py:66-119,280-323` |
| A negative holding | The monitor sets `exit_side = BUY`, and the gateway classifies a buy against a negative position as a protective exit. **The short is covered automatically.** | `monitor/coordinator.py:498-499`, `execution/gateway.py:266-267` |
| A trade outside TradePulse | Reconciliation reports an activity with no local fill as a missed fill and latches `FINANCIAL_INTEGRITY_BLOCKED`. A broker position without a holding is alerted as unmanaged. | `reconciliation/coordinator.py:440-468`, `monitor/coordinator.py:446-452` |
| Equity identity | `valuation.py` includes `short_market_value` in the broker identity, which is already correct for a negative position | `valuation.py:210-218` |

**Evidence.** No database has ever held a short lot: the six preserved soak databases and the service database `tradepulse.db`, all opened read-only. Every change below is therefore invisible to existing ledgers.

**The gaps:**
1. The gateway does not itself net pending sells; a future caller could bypass the monitor's guard.
2. The gateway does not consult broker open orders, including orders placed outside TradePulse.
3. An oversell becomes an ordinary short lot and a negative holding.
4. The monitor would automatically cover it.
5. Nothing raises an integrity hold for it.

## 2. Requirements (operator decisions C10, D6)

1. Prevent internal orders exceeding verified sellable inventory, net of outstanding reservations and pending sells.
2. Preserve an actual broker oversell whole: the fill, cash proceeds, execution identifiers and fees.
3. Close only verified long inventory, using the existing lot rules.
4. Record the excess as a quarantined short-exposure liability, not a tradable lot.
5. Include it in equity, mark-to-market valuation, reconciliation and conservative risk.
6. Keep realized P&L on the excess unrecognized until it is economically closed, or broker evidence establishes its disposition.
7. No automatic trading against it.
8. An integrity hold, released only by controlled reconciliation.
9. Never discard a real execution, fabricate inventory, convert a negative quantity to long, or erase it.
10. Deterministic tests for:
    - partial fills;
    - overlapping sells;
    - a broker-side oversell;
    - a trade outside the account;
    - settlement recovery;
    - liability valuation;
    - disposition.

## 3. Design

### 3.1 Prevention in the gateway

A sell's **sellable quantity** is computed inside the gateway, under the asset's execution reservation, from:

  sellable = broker held quantity − Σ unfilled quantity of this asset's other SELL intents in an in-flight or `submission_unknown` status − Σ unfilled quantity of broker open SELL orders for this asset with no matching intent

- **Sources:**
  - `get_positions()`, already fetched;
  - `get_open_orders()`, one more broker call, made before the decision window as A1 6.3 requires;
  - the trade-intent store.
- **Matching:** a broker open order is matched to an intent by `client_order_id`, which equals the intent ID. An unmatched open order is out-of-band: it is subtracted, and it is alerted.
- **Refusals:**
  - a sell above sellable is refused: `INSUFFICIENT_SELLABLE_QUANTITY (held h, pending p, requested r)`;
  - a sell when held ≤ 0, or a buy classified as protective against a negative position, is refused: `UNSUPPORTED_SHORT_POSITION`. This removes the cover path from the gateway.
- **Failure:** if `get_open_orders()` fails, the sell is refused (`BROKER_OPEN_ORDERS_UNAVAILABLE`), as today for unavailable positions. The monitor retries next pass.
- **Partial fills:** an intent's remaining quantity is its approved quantity minus settled fills, so a partially filled sell reserves only what is left.

### 3.2 Settlement of an oversell

`plan_signed_lot_fill` is unchanged; it still reports the excess as `opening_quantity` in the opposite direction. In `_project_lot`:
- **A SELL** whose plan opens a `short` quantity creates **no lot.** It creates a `ShortExposureLiability`, with `create_once` unique on `fill_id` so a resumed run is a no-op. The liability records:
  - `liability_id`, `fill_id`, `broker_order_id`, `trade_intent_id`, the asset;
  - `quantity` (the excess) and `sale_price` (the fill price);
  - `proceeds` = quantity × price × multiplier;
  - `opened_at`, status `quarantined`, the hold reason;
  - `fee_ids`, linked later (3.6).
- **The closing part** settles exactly as today: lot closures, realized P&L and attribution.
- **Cash:** `project_cash` is unchanged and books the full fill.
- **Holdings:** `_project_holding` is unchanged. It sums open lots, which are now long only. A holding fully closed by the oversell is deleted, as for any close.
- **The hold.** After `verify_integrity`, settlement:
  - calls `latch_financial_integrity_block(reason="UNSUPPORTED_SHORT_POSITION: <liability_id>")`, which is idempotent;
  - writes an integrity hold on the broker order;
  - sends a critical alert, once per liability.
  The settlement event itself completes, so the ledger is whole and a replay is a no-op.
- **A BUY** whose plan would close `short` lots: none can exist (section 1). If one ever does, it raises `INTEGRITY_VIOLATION: UNSUPPORTED_SHORT_LOT` (`INTEGRITY_BLOCKED`) instead of trading against it.

### 3.3 Valuation, exposure and risk

- **Local position value:** the liability is valued at quantity × current mark and *subtracted* from local holdings value. Local equity = cash + long holdings value − liabilities.
- **Unrealized P&L** on the excess = proceeds − current liability value. It is shown in the dashboard and in the equity snapshot (`short_exposure_liabilities` block).
- **Risk snapshot:** exposure counts the liability at its absolute value, at the higher of the last confirmed and the current mark. Entries are refused anyway while the latch holds.
- **Reconciliation of the valuation:** the broker's `short_market_value` must equal −Σ liability value within the broker's cent rounding, the tolerance `valuation.py` already reproduces.

### 3.4 The monitor

- A negative broker position, or one with a quarantined liability, gets **no exit of any kind:** no stop, ladder, time stop or forced close.
- Each pass alerts `UNSUPPORTED_SHORT_POSITION` through `_alert_once`, rate-limited per liability per day.
- Long positions in other assets continue as today.

### 3.5 Reconciliation

- **Quantity:**
  - the broker's position quantity must equal Σ open long lots − Σ quarantined liability quantity, exactly;
  - any broker position < 0 with no liability, or any non-zero `short_market_value` without a liability, latches `FINANCIAL_INTEGRITY_BLOCKED` (`UNEXPLAINED_SHORT_POSITION`).
- **No liability is fabricated** from a position read. A liability is created only from the authentic execution, when settlement or late-fill recovery processes it.
- **A trade outside TradePulse** is today's missed-fill path. It already latches; Rev.140 adds only the position-level check above.

### 3.6 Fees

- Alpaca posts equity fees as one daily batch per kind (A1 12.6), and those batches are attributed exactly as today.
- The excess quantity's share of the oversell fill's fees, by the existing attribution rule, is linked to the liability (`fee_ids`). It is posted to the ledger like any fee, and recognised in the excess's P&L only at disposition.

### 3.7 Disposition: controlled reconciliation only

Only the operator command `tradepulse liability resolve <liability_id> --activity <broker activity id> --reason "<text>"` resolves a liability.
- **It verifies the evidence before doing anything:**
  - the activity exists in broker activities;
  - it is a BUY of the same asset, after the liability opened;
  - the quantities agree.
  A broker buy-in or journal is accepted only with its activity ID.
- **It imports the covering execution** as an accounted fill through the existing late-fill recovery, so its cash is booked once.
- **It closes the liability** oldest-first and books its realized P&L once: proceeds − cover cost − attributed fees, with idempotency key `liability:<liability_id>:<activity id>`.
- **It writes** an audit event, and `resolved_by` evidence on the liability.
- **`reset-integrity`** refuses while any liability is `quarantined`.

Rev.140 sends no cover order. The operator closes the position at the broker, and the command accounts for it.

### 3.8 Schema

One new payload table, `short_exposure_liabilities(record_id, payload, created_at)`, with `UNIQUE(fill_id)`.
- It follows the existing payload-table pattern.
- It is created with `CREATE TABLE IF NOT EXISTS`, so it is additive and needs no rebuild or migration step.
- A1 adds `account_key` to it (A1 Appendix B).

## 4. Behaviour changes in the live runtime

| Change | When it is visible |
|---|---|
| Sells are netted against pending sells and broker open orders | Only when another sell is pending. Today's monitor guard already prevents this, so normally never. |
| One more broker call (`get_open_orders`) per sell | Every exit; one request, within the rate limit |
| `UNSUPPORTED_SHORT_POSITION` refusals; no automatic cover | Only with a negative position: never so far |
| An oversell becomes a liability and a latch, not a short lot | Only on an oversell: never so far |
| A new table; equity snapshots gain a block that is empty in normal operation | Always present, normally empty |

Replaying every preserved soak database must give identical ledgers and decisions.

## 5. Tests (deterministic, on `SimulatedBroker` with its clock)

1. **Partial fills:**
   - a sell of 10 against 10 fills 4, then 6;
   - in between, a second sell of any size is refused `INSUFFICIENT_SELLABLE_QUANTITY`;
   - after the second fill, sellable is 0.
2. **Overlapping sells:**
   - two exits for the same asset race (monitor guard bypassed, called directly against the gateway);
   - exactly one is approved;
   - a broker open sell with no intent (placed out-of-band in the simulator) reduces sellable, and is alerted.
3. **Broker-side oversell:**
   - a fault-injected fill of 12 against a held 10;
   - 10 closes the lots with realized P&L, 2 becomes a quarantined liability, cash has the full proceeds;
   - the session is `FINANCIAL_INTEGRITY_BLOCKED`, and no lot is short.
4. **A trade outside the account:**
   - an out-of-band sell in the simulator takes the account to −3;
   - reconciliation latches `UNEXPLAINED_SHORT_POSITION`;
   - no liability is created until the fill is recovered, then exactly one.
5. **Settlement recovery:** a crash after `project_lot` and before `project_cash` for the oversell; on resume, one liability, one cash entry, one latch.
6. **The monitor:** with a negative position, no order of any kind is sent across several passes; the alert is once per day; another asset's stop still fires.
7. **Liability valuation:**
   - the mark moves up, then down;
   - local equity, unrealized P&L and exposure move with it;
   - the broker identity with `short_market_value` holds to the cent.
8. **Disposition:**
   - `liability resolve` with a valid covering activity books realized P&L once; running it again changes nothing;
   - it refuses a wrong asset, a wrong side, an earlier activity or a missing one;
   - `reset-integrity` refuses until it is resolved.

Each test asserts no float in any financial value (Decimal throughout), and that no broker order is sent except where the test intends.

## 6. Open questions

- **R1. A partial cover.** When the covering activity is smaller than the liability, should the command resolve partially or refuse? **Proposed:** partial, oldest-first; the remainder stays quarantined and the latch stays set.
- **R2. Does Alpaca refuse a long-to-short sell?** Whether Alpaca itself rejects a sell larger than the long position, or opens the short, is not verified for equities, options or crypto. It would mean the broker-side oversell can only come from a broker fault or an out-of-band order.
  - **Proposed:** no reliance on it. If verified, it is recorded as evidence only.
  - Verifying it would place a paper order, so it needs explicit approval and the account lease. It is not done here.
- **R3. The excess's fee share.** **Proposed:** a pro-rata quantity share of the fill's attributed fees, by the existing attribution rule. Confirm.

## 7. Evidence required for approval

1. The A0 verification run complete, its criteria decided, and its evidence preserved with SHA-256.
2. This design approved, with R1 and R3 decided.
3. The eight tests, the full suite, mypy and ruff green.
4. Every preserved soak database, and a copy of the service database, replays to identical ledgers.
5. The operator's review of the Rev.140 zip before merge.
