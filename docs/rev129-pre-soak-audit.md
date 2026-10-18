# Rev.129: pre-soak audit of Rev.128

Rev.129 follows Rev.128 (`653d3d06`). It comes from a fresh audit before the 120-hour soak 1 (Tue 2026-10-20 06:30 PT). Protected source changed, so the soak freezes Rev.129.

## A. A skipped scale-out was never retried (fixed)

The monitor marks a holding `scaled_out` and moves its stop to break-even before submitting the half-sale; that ordering is what prevents a double scale-out. But if the gateway then skipped or rejected the sale with nothing filled (for example a stale quote in the first minutes of the session), the mark stayed set and the partial profit was never taken.

Now the monitor clears `scaled_out` when the half-sale is skipped or rejected with zero fill, and keeps the break-even stop, so the next cycle retries. A pending order is already guarded by the in-flight check.

Test: `test_a_scale_out_whose_sale_is_skipped_is_retried_next_cycle` (stale quote) failed on Rev.128.

## B. Exit reasons were unrecoverable for Phase 1 exits (fixed, data quality)

`TradeAttribution.exit_reason` is inferred afterwards from the exit price against the opening intent's original stop and target. Break-even and trailing-stop exits above the original stop, and every exit-ladder exit, are recorded as `other`, so learning data could not separate profit-taking, stalled trades and protective exits.

Now `ExecutionRequest.exit_reason` carries the monitor's actual reason into the closing intent's `risk_snapshot["exit_reason"]` (only when set, so buy snapshots are unchanged). The reasons are:

- `stop_loss`, `break_even_stop`, `trailing_stop`, `target_price`
- `option_expiry`, `time_stop`
- `scale_out`, `target_r`, `target_r_single_unit`, `stall`

Attribution's inference, and the fee-replay path that recomputes it, are deliberately unchanged, so projection and replay cannot disagree. Analysis should read the closing intent's recorded reason.

Tests: `test_exit_reason_names_the_rule_that_fired`, and the stop-breach exit test asserting the recorded reason (which failed on Rev.128).

## Checked, no change

- Static analysis for real bugs (pyflakes and bugbear classes) is clean. The one hit is an unreferenced task that opens a browser and never runs in soaks (`--no-browser`).
- No code refers to the removed win-rate criterion. The `trades_today` producers and consumers are consistent.
- The monitor's ordering is right:
  - an out-of-session deferral happens before any ratchet or extreme folding;
  - the ladder is evaluated only when no stop, expiry or time exit applies;
  - the break-even lock comes before the partial sale.
- Open risk and per-symbol entries are computed only for new BUYs, inside the portfolio-risk lock. Protective exits bypass every entry limit.
- The cost rule at current volatility: BTC's 2× ATR stop (about 5.1%) allows 0.51% of costs against 0.50% of entry-tier fees plus spread, so BTC is effectively blocked. ETH (7.5%) and SOL (9.8%) pass. This is intended, and left to revisit with data.
