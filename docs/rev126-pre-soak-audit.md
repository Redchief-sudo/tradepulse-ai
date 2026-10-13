# Rev.126: pre-soak audit fixes

Rev.126 follows Rev.125 (`b6f6c66c`). It comes from a fresh audit of the money math and long-run behaviour before the 72-hour soak 1. Both fixes change protected source, so the soak freezes Rev.126.

## 1. Medium: add-on buys could risk more than their budget

**Finding.**
- Each BUY is sized so that its own stop loses at most `max_risk_per_trade_pct` (0.30% on `balanced`) of equity, scaled by confidence and regime.
- Once a symbol is held, the monitor enforces a single stop: the first entry's (`PROTECTIVE_THRESHOLD_POLICY = "first_entry"`).
- An add-on whose own ATR stop is tighter than that enforced stop was therefore sized for a smaller loss than it would really take.
- In soak 1 the second AMZN and UNH buys happened to have looser stops than the enforced ones, which is the safe direction.
- In the reproduction (5 shares held with a $150 enforced stop, then a BUY at about $199.55 with its own $195 stop), the add-on was sized at 35.07 shares. Stopped out at $150, that loses about $1,738, or 1.7% of equity against a 0.3% budget. The 7% position cap was the only limit.

**Change.** When a BUY adds to a held long, the gateway sizes it against the looser of its own stop and the holding's enforced stop. The intent still records its own stop.

**Validation.** `test_add_on_buy_is_sized_against_the_holdings_enforced_stop` gives at most 6.06 shares. It failed on Rev.125 (35.07).

## 2. Medium (long runs): every valuation decoded the whole reconciliation table

**Finding.** `_generation_invariants` (run by `marked_snapshot` on every reconcile pass, every scan and a dashboard endpoint) JSON-decoded every reconciliation record to find the newest generation-membership record. Reconciliation writes a record per checked fill on every pass, so the cost grew all run. It was 1.46 s on soak 1's 78 MB database: the same CPU and GIL pattern behind Rev.124's lock errors, at a smaller scale.

**Change.** The newest membership record is selected in SQL, and the `generation_fee:` / `generation_adjustment:` records it names are fetched by primary key. A missing record still raises `KeyError`, as before.

**Validation.** On a copy of soak 1's database, 1.460 s became 0.346 s, with byte-identical output. Full suite passes.

## Observations, not changed

- **The daily trade cap counts exits.** Protective exits are never blocked by `max_daily_trades`: they return before the check. But `trades_today` counts every filled intent, exits included, so exits reduce the day's room for entries. This is a design choice to revisit with the strategy, not a defect.
- **Fill reconciliation records grow with the run.** Each pass writes a MATCHED record per fill in the lookback window, about fills × 1,440 rows a day. With the Rev.124 and Rev.126 fixes, nothing reads them per pass in a way that grows with the run. Revisit before the 60-day prove-edge run.
- **Checked and sound:**
  - Exits: the stop breach, the monotonic break-even and ATR trailing ratchet, the time stop counted from the oldest lot, and the forced close of options near expiry.
  - Sizing: the confidence and regime multipliers only shrink the budget; the contract multiplier is applied before dividing; the position, sector, exposure and cash caps only ever reduce size.
  - Lots: FIFO lot closing, P&L with the contract multiplier, and replay-safe lot closure.
