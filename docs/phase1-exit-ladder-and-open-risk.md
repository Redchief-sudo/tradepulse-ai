# Phase 1: profit-aware exit ladder and combined open-risk limit

Chosen by the operator on 2026-10-15 after the Rev.127 soak showed a swing strategy that seldom exits and is capped at 3 entries a day. The goal is a design that works the same, in percentage terms, for any account size, and does not starve on trades. Protected source changed, so the soaks re-run on this revision.

## Principle

Everything is a percentage of equity or a multiple of **R**, the position's own risk per unit (average price − first-entry stop). Sizing on current equity compounds automatically, so a $500 account and a $500,000 account get the same percentage result. Per-trade risk stays at 0.30% of equity (`balanced`): the edge is unproven and prove-edge fails at 10% drawdown.

## Exit ladder (`monitor/coordinator.py::plan_ladder_exit`)

Evaluated every monitor cycle, after the existing stop and trailing stop, option expiry and 15-day time stop, which take precedence:

| Condition | Action |
|---|---|
| +3R (`final_target_r`) | sell everything |
| +1R (`scale_out_r`), not yet scaled out, splittable | move the stop to at least break-even and mark the holding `scaled_out`, then sell half (`scale_out_fraction`) |
| +1.5R (`single_unit_target_r`), cannot be split | sell everything (a single option contract, or a part below order precision or the $1 minimum lot) |
| held ≥ 7 days (`stall_exit_days`) without the best price ever reaching +0.5R (`stall_min_r`), not yet scaled out | sell everything: the trade is not working |

The stop is moved and `scaled_out` recorded in one atomic holdings mutation **before** the sale. If the sale fails, the position is still protected at break-even and is never scaled out twice. Settlement carries `scaled_out` forward across holding rebuilds, as it already did for `current_stop`. The remaining half exits on the trailing stop (2.5× ATR), at +3R, or on the time stop.

Break-even win rate before costs: about 40% for a 1.5R full exit against a 1R stop. Scale-out ladders typically need 35–45%, depending on how far the remainder runs. "Sell on any profit" would need about 88% with a 0.5% target against a 3.5% stop.

## Exits act on regular-session prices (stocks and options)

Found in the Rev.127 soak. On 2026-10-15 from about 03:00 UTC, SPY's overnight print was below its $766.21 stop (as low as $765.76) for 122 consecutive snapshots. Alpaca's position price keeps moving with overnight trading, but there was no fresh executable quote, so every exit the monitor attempted was refused by the gateway's 30-second quote-age check. This was silent, with no alert, and SPY was back above its stop by the open.

Now, while Alpaca's clock says the stock market is closed, stock and option holdings are not ratcheted, their price extremes are not updated, and no exit is attempted. A breach seen then is recorded and alerted once per symbol per UTC day (`exit_deferred_market_closed`, through `alert_once`, so it is recorded even if Telegram is down), and counted in `MonitorCycleSummary.deferred_exits`. The first in-session cycle sells if the price is still past the stop. Crypto is managed around the clock. If the clock cannot be read, the previous behaviour applies.

## Entry limits (`risk/engine.py::evaluate_risk`)

| Setting (`balanced`) | Before | Now |
|---|---|---|
| `max_daily_trades` (buys per risk day, Rev.127) | 3 | 10 |
| `max_open_positions` | 5 | 8 |
| `max_total_exposure_pct` | 40 | 60 |
| `max_open_risk_pct` (new) | none | 3 |
| `max_entries_per_symbol_per_day` (new) | none | 2 (an entry plus one add-on) |
| `max_cost_to_risk_pct` (new) | none | 10 |

- **Combined open risk** (`open_portfolio_risk`): what every open long and every pending buy stands to lose at its stop, Σ max(0, average − effective stop) × quantity × multiplier. A buy is sized down so the total stays within 3% of equity. A holding whose effective stop is at or above its average price counts as zero, so scaled-out winners free room for new trades: the brake is risk, not a position count.
- **Per-symbol entries** (`symbol_entries_today`): distinct filled BUY intents for the symbol in the New York risk day. This stops the repeated same-name add-ons seen in the Rev.127 soak.
- **Cost rule:** a buy is rejected if its estimated round-trip cost (full spread, plus 0.25% per side for crypto taker fees) exceeds 10% of its own R. Friction then cannot consume the edge at any account size.

Protective exits bypass every entry limit.

## Prove-edge criteria (`verification/evidence.py::VerificationPolicy`)

- At least 100 completed round trips, down from 200. A swing strategy cannot close 200 in 60 days: the 3-a-day cap alone allowed at most 126 stock entries.
- Net expectancy above 0, **and its 95% lower bound** (mean − 1.96·s/√n over per-trade net results) above 0.
- The fixed 55% win-rate gate is removed, because an R-ladder can be profitable below 50% wins. The win rate is still reported (`win_rate_pct`).
- Duration (60 days), drawdown (10%), net P&L and every integrity gate are unchanged.

A scaled-out position is one round trip: assessment already groups all of an opening intent's lots and closures.

## Validation

- `test_exit_ladder.py`: every rung, fractional rounding, option splitting, the minimum-lot single-unit case, the stall boundary, and disabling each rung.
- `test_monitor_coordinator.py`: an end-to-end scale-out (stop to break-even and `scaled_out` recorded, then a SELL for half), and no second scale-out. Earlier monitor tests run with the ladder off, so they keep testing what they were written for.
- `test_risk_engine.py`: the open-risk cap and its exhaustion, the per-symbol limit, the cost rule (crypto rejected, equity accepted), protective exits bypassing every limit, and both helpers.
- `test_settlement_engine.py`: `scaled_out` survives a rebuild in which the governing lot changes.
- `test_paper_verification.py`: the 100-trade boundary, and the expectancy lower-bound gate (80 of 200 wins: the mean passes, the lower bound fails).
- Full suite passes.

## Phase 2 (not built)

- Account-size tiers: options only when one contract's risk fits the budget; positions of at least $10 under $2k.
- A pattern-day-trader guard for margin accounts under $25k: profit exits wait for the next session, stops always fire, one day trade is kept in reserve.

These matter before a small account trades live, not for the paper soak.
