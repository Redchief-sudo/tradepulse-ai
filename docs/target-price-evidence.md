# Take-profit target: empirical evidence (no production change)

**Read-only.** No `tradepulse/` file is touched by this pass, and no `target_price` mechanism is proposed for production here. `target_price=null` on every current holding was already confirmed intentional architecture, not a defect: fully wired through `models`/`execution`/`settlement`/`monitor`, but never computed anywhere in `scanner/coordinator.py` for any asset class. Turning it on would be a strategy-design change, not a bug fix -- so per this project's own evidence-before-guessing discipline (the Generation-2 freeze, the held-out entry-composite ladder), this pass gathers evidence on what a take-profit target would actually do, before any specific R-multiple or sizing method is chosen. Deterministic-layer-only, same permanent limitation as every other tool in this series: no AI-recommendation gate is simulated.

## Why this pass exists

Three questions were posed before any target-sizing method should be picked: how often does price reach 1R/1.5R/2R/2.5R/3R before the current policy's own exit fires, how much of that profit does the current trailing/break-even/time-stop policy end up giving back, and how would a hard, full-exit target at each R candidate compare against the current policy on the exact same walk-forward folds already approved for exit-parameter calibration. ("1R" = entry price minus the same ATR-based initial stop the ladder/exit-param calibration already uses.)

## Methodology

- Reuses `simulate_trades.py`'s entry generation directly (same fixed-baseline composite, same ATR stop, same no-lookahead machinery) -- no new entries, no new data fetch.
- **Milestone evidence** (§1/§2): for every entry, the current policy's own exit (`simulate_exit`) is computed once, then the bar range up to that exit is checked for whether that day's HIGH ever touched `entry + R*risk`, for R in {1, 1.5, 2, 2.5, 3} -- the upside-symmetric counterpart of `simulate_exit`'s own low-touches-stop convention. Never looks past the baseline's own exit date.
- **Policy comparison** (§3): `simulate_exit_with_target` is a field-for-field mirror of `simulate_exit` with exactly one addition -- a full exit the first day price's HIGH touches the target. Evaluated on `calibrate_exit_params.FOLDS` (the same four walk-forward folds already approved for exit-parameter calibration) and the same frozen friction scenarios, for R in {1.5, 2, 2.5, 3}, against the `baseline_trailing_only` policy (today's actual behavior).
- New code: `tools/historical_data/target_price_evidence.py`. Results: `data/calibration/target_price_evidence.json`.

## §1/§2: how often is each milestone reached, and how much is given back after

| R | Equity reached (n=6,499) | Equity avg give-back when reached | Crypto reached (n=81) | Crypto avg give-back when reached |
|---|---:|---:|---:|---:|
| 1.0 | 33.1% | -0.06R | 29.6% | -0.08R |
| 1.5 | 17.0% | -0.09R | 23.5% | +0.15R |
| 2.0 | 7.6% | -0.12R | 22.2% | +0.58R |
| 2.5 | 4.2% | +0.01R | 21.0% | +1.11R |
| 3.0 | 1.9% | +0.28R | 13.6% | +2.00R |

Negative give-back means the current policy's *actual* exit r-multiple was, on average, **better** than the milestone it passed through -- i.e. letting it ride paid off. For equity, that holds up through roughly 2R: the trailing/break-even policy is already capturing more than a fixed exit at 1-2R would have, on average. Beyond 2.5-3R, give-back turns positive -- the rare biggest equity winners do surrender some profit before the trailing stop finally closes them. For crypto, give-back turns positive much earlier (already at 1.5R) and grows fast -- consistent with crypto's wider ATR trail (`trailing_atr_multiplier`) giving back more before it catches a sharp reversal, though the sample here (81 entries total, single digits to low 30s per fold) is thin.

## §3: does a hard target beat the current policy? (walk-forward, base friction)

| Fold | Equity baseline exp_R | Best equity target | Crypto baseline exp_R | Best crypto target |
|---|---:|---|---:|---|
| fold_1 (2023) | **-0.063** (best) | none beat baseline | -0.497 | target_1.5R (-0.466), still worse than baseline's own PF |
| fold_2 (2024) | -0.013 | **target_3R: +0.013**, PF 1.03 | 0.496 | **target_1.5R: +0.798**, PF 5.32 |
| fold_3 (2025 H1) | **-0.142** (best) | none beat baseline | -0.370 | target_2.5R (-0.278), still negative |
| fold_4 (2025 H2+) | **-0.067** (best) | none beat baseline | -0.803 | target_2.5R (-0.746), still negative |

## Interpretation

**No target-R candidate shows a stable improvement over the current pure-trailing policy across folds, in either asset class.** In equity, some target beat baseline in exactly one of four folds (fold_2, where `target_3R` edged out baseline); in the other three, the current no-target policy was the best or tied-best performer. In crypto, `target_1.5R` looked dramatically better in fold_2 (+0.798R vs +0.496R baseline) but that's 17 trades in one fold -- nowhere near enough to trust, and the other three crypto folds show no target rescuing an already-negative baseline.

This is exactly the fold-instability pattern the entry-composite forensic audit and calibration ladder were built to catch: a result that looks good in one slice and doesn't hold up in the others is not evidence of a real edge, it's noise (or a regime-specific effect, given fold_2 covers the 2024 period) -- picking a target-R based on fold_2 alone would be the same mistake the earlier composite audit warned against for entry weights.

## Bottom line

**No production change follows from this pass**, consistent with the confirmed-intentional status of `target_price=null`. The evidence does not currently support adding a fixed hard-exit take-profit target at any single R-multiple -- the current trailing-stop/break-even/time-stop policy is already competitive with or better than every target candidate tested, in 3 of 4 equity folds and 3 of 4 crypto folds. The one place a target showed real promise (crypto, fold_2) is too thin a sample to act on. If this is revisited, the more promising direction per the milestone data (§1/§2) is not a full-exit target at all, but a **partial scale-out** past ~2.5-3R specifically for equity (where give-back turns positive) -- TradePulse has no partial-fill/scale-out mechanism today, so that would be new execution-layer work, not just a new price field, and should wait for real closed-trade `TradeAttribution` data from the live paper run rather than more backtested evidence alone.

## Verification

- `git status`/`git diff --stat`: zero changes under `tradepulse/` -- only a new file under `tools/historical_data/` and this doc.
- No commit, no push yet (per the established workflow, cut once reviewed).
