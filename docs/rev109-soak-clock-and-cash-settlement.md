# Rev.109: soak broker-clock skew and per-fill cash settlement

Rev.109 follows Rev.108 (`d5ef800`). It carries the pending integrity-reset/legacy-evidence work already in the tree, plus two corrections found by examining the failed accounting soak 1 (`data/soaks/20260925T043540Z/`, preserved, not modified).

## Findings from soak 1

The soak ran 12 hours and failed for two independent reasons, neither caused by trading behaviour.

1. **`invalid_soak_broker_clock`.** The report builder rejected any broker clock receipt whose server `timestamp` was later than the local `received_at`. Alpaca's server clock and the local clock are independent; across 682 receipts the broker led local receipt by up to 45 ms, and 154 honest receipts were rejected, aborting report construction.
2. **Generation cash mismatch of `-0.00344`.** From the first equity fill onward, every equity reconciliation was `incomplete_evidence`. An 8.124-share AAPL order filled in four executions; the ledger recorded the exact `0.124 x 335.94 = 41.65656`, while Alpaca debited `41.66`. Alpaca settles each fill's cash to the nearest cent.

The per-fill rule was checked against every broker cash-snapshot interval (legacy database and soak 1) that contained a fractional-cent fill and only receipt-bearing non-fill activity: per-fill nearest-cent rounding matched all 15 equity and crypto intervals; the 16th differs only by a later-posted crypto fee, which the existing fee path handles. Exact amounts and broker-favourable truncation do not match. No exact half-cent tie was observed, so half-up versus half-even is not distinguishable from evidence; a wrong tie rule would fail closed as a one-cent mismatch.

## Corrections

- `tradepulse/verification/soak.py`: a broker clock `timestamp` may lead `received_at` by at most `BROKER_CLOCK_SKEW_TOLERANCE_SECONDS = 5`. Materially future stamps are still refused. Market-session coverage selects receipts by their own timestamp and receive time within the segment rather than requiring `timestamp <= received_at`.
- `tradepulse/valuation.py`: `broker_settled_cash` applies per-fill cent settlement only when comparing the ledger to broker cash. The immutable `fill:cash:` ledger entries, lots, attributions and P&L keep their exact amounts. Non-fill entries are broker-reported cent amounts and pass through unchanged.
- `tradepulse/config/logging.py`: `httpx`/`httpcore` loggers are held at WARNING. Their INFO request lines contain the full URL, and Telegram Bot API URLs embed the bot token.

No thresholds, fee/slippage overlay rates, risk parameters or trading logic changed.

## Operational notes

- Soak 1 and the legacy `tradepulse.db` used the same Alpaca paper account. The soak's GOOGL/SPY sells and AAPL buy therefore appear as unexplained drift in the legacy database, which remains `FINANCIAL_INTEGRITY_BLOCKED`. Per `paper-verification-integrity.md`, that latch is preserved as legacy evidence and is not a prerequisite for a new soak.
- Because the protected source changed, both soaks must be run (again) from Rev.109 with new database/report paths before an official freeze.
- Earlier runtime logs contain the Telegram bot token in request URLs; the token should be rotated.

## Validation

Complete Python suite passed. New regression coverage: bounded broker-clock lead accepted and future receipts refused; the real soak AAPL receipts reproduce broker cash exactly; symmetric per-fill rounding with non-fill entries unchanged; transport loggers suppressed at INFO.
