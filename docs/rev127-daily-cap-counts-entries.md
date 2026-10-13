# Rev.127: the daily trade cap counts entries only

Rev.127 follows Rev.126 (`59699c52`). It is an operator-chosen strategy change, made before the 72-hour soak 1. Protected source changed, so the soak freezes Rev.127.

## Change

`max_daily_trades` (3 on `balanced`) now limits new positions only. `trades_today` counts the distinct BUY intents filled in the New York risk day. TradePulse opens long positions only, so a BUY is an entry and a SELL is an exit.

Before, `trades_today` counted every filled intent, so stop-outs, trailing-stop exits, time stops and forced option closes used up the day's allowance for entries. Protective exits were already never blocked by the cap, since they return before the check.

Both producers changed:
- the risk snapshot (`risk/engine.py::build_portfolio_snapshot`), which enforces the cap;
- the recorded valuation snapshot (`valuation.py`), shown on the dashboard, where the field is now labelled "Buys today (daily cap)".

The field name `trades_today` is unchanged.

## Validation

`test_trades_today_counts_entries_not_exits` (one buy and two sells counts 1) failed on Rev.126 (3). The existing distinct-intent, prior-day and NY-midnight boundary tests, the full Python suite and the frontend tests pass.
