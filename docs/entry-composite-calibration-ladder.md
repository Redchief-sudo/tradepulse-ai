# Deterministic entry-composite held-out calibration ladder

**Read-only.** No `tradepulse/` file is changed by this pass -- no weight, threshold, or factor formula is touched in production. This document reports calibration *evidence* and a *candidate recommendation*; applying it is a separate, later, explicitly-approved step. Deterministic-layer-only, same as every prior document in this series: no AI-recommendation gate is simulated, because historical AI opinions cannot be reconstructed -- nothing here is a claim about historical performance of the full AI + deterministic TradePulse pipeline.

## Why this phase exists

Rev.87's forensic audit confirmed Rev.86's finding and identified the mechanism: `risk_score` (a calmness measure) is negatively correlated with forward returns and, combined additively with `technical_score`/`momentum_score` into a "higher = stronger BUY" composite, actively rewards the wrong combination in both asset classes. The user approved this held-out calibration phase with an explicit **ablation ladder** (B0-B7) -- testing one narrow, isolated correction at a time rather than searching the whole parameter space simultaneously, which would very likely overfit the same discovery dataset that exposed the problem -- plus five additional controls: (1) the HOLDOUT pool is single-use for this calibration generation; (2) non-regression guards (expectancy R / profit factor / max drawdown / trade count) gate every promotion, not just Spearman; (3) `risk_score` is not forced to survive B3 if no gate variant clears the bar; (4) a B0 parity proof must pass before any calibration result is trusted; (5) TRAIN/VALIDATION/HOLDOUT isolation is enforced structurally in code and tested directly.

## Methodology

- **Pools** (non-overlapping, reusing the same chronological cutoffs already approved for the exit-parameter calibration): TRAIN = 2023-01-01 to 2024-12-31; VALIDATION = 2025-01-01 to 2025-08-31; HOLDOUT = 2025-09-01 onward. HOLDOUT is loaded only once, in the final step -- never passed into any selection/promotion function (structurally proven in `test_entry_calibration_ladder_harness.py::test_evaluate_candidate_default_pools_exclude_holdout`).
- **Promotion rule** (primary): TRAIN-pool composite-vs-5-day-forward-return Spearman must improve by &ge;+0.02 absolute vs. the current frozen rung.
- **Non-regression guards** (pass/fail, never optimization targets): a rung is not promoted if, vs. the prior frozen rung, TRAIN trade count collapses below 20% of its prior value, TRAIN expectancy R flips from positive to negative, TRAIN profit factor drops below 0.5, or TRAIN max drawdown more than doubles.
- **B3/B7 exception**: threshold- and gate-defining rungs are selected by TRAIN expectancy R, not Spearman (a threshold/gate doesn't change the composite's ranking, only which points are labeled "enter" -- Spearman is identical across every threshold candidate and cannot distinguish between them).
- All new primitives (raw sub-indicator extraction, the Spearman/decile helpers, the pool partition, the gate/decomposition/normalization candidates) are covered by `python_tests/test_entry_calibration_ladder_harness.py` (17 tests) and reuse `tools/historical_data/entry_composite_audit.py`/`simulate_trades.py`/`diagnose_signal_sparsity.py`'s existing primitives wherever the computation is identical to what those scripts already do.

## B0 parity proof

**Passed.** The generalized ladder harness's B0 candidate (built through the same new entry/exit pipeline every other rung uses) reproduces `diagnose_signal_sparsity.generate_daily_samples`'s existing, already-verified composite/signal output **exactly** -- 62,484 samples checked, 0 mismatches. This is a structural proof, not a spot check: every sample's `composite`/`signal` was compared field-by-field. Per the mandated control, B1-B7 selection only proceeded after this passed.

## Ladder results

Pools: TRAIN = 2023-2024 (n&asymp;20,807 samples with a valid 5-day forward return), VALIDATION = Jan-Aug 2025 (n&asymp;6,990), HOLDOUT = Sep 2025+ (untouched until the final step).

| Rung | Change | TRAIN Spearman &Delta; | Promoted? | Reason |
|---|---|---:|---|---|
| B0 | baseline | -- | -- | mandatory baseline (TRAIN Spearman -0.0841, VALIDATION -0.0942) |
| B1 | calendar-aware risk_score annualization | +0.0060 | **No** | below the +0.02 threshold |
| B2 | drop risk_score from the composite | +0.0332 | **No** | Spearman cleared the bar, but the non-regression guard caught it: TRAIN expectancy_r flipped **positive to negative** (+0.0052 &rarr; -0.1010) |
| B3 | risk_score as a gate | -- | **Skipped** | B2 wasn't promoted, so per the plan's explicit rule B3 is not evaluated -- frozen as "risk_score excluded from directional entry selection" does not apply either, since B2 itself didn't survive; risk_score remains additive, unchanged from B0 |
| B4 | technical decomposition (mean_reversion / trend_confirmation / avg_decomposed) | +0.0179 (best: mean_reversion alone) | **No** | below the +0.02 threshold |
| B5 | momentum normalization (linear_x2 / calendar_rescaled / percentile_rank) | +0.0008 (best: percentile_rank) | **No** | below the threshold, and the percentile-rank transform's own guards failed (expectancy flipped positive-to-negative; max drawdown more than doubled, driven by a large increase in trade count) |
| B6 | technical/momentum weight grid (11 points, risk_weight held at 15) | **+0.0450** (winner: technical=100, momentum=0) | **Yes** | clears the bar, guards pass |
| B7 | BUY-threshold grid (50/55/60/65/70/75), selected by TRAIN expectancy_r | expectancy 0.0839 &rarr; 0.1169 | **Yes** | improves TRAIN expectancy with guards passing |

**B2 is the single most important intermediate result in this run.** It is the one candidate that would have looked attractive by the ordering metric alone -- and the non-regression guard, exactly as specified, caught that its actual tradeable expectancy was worse, not better. This is direct, concrete validation of the "guards, not just Spearman" control.

B6's winning grid point set `technical_weight=100, momentum_weight=0` -- momentum contributed nothing useful in this search once technical was allowed to dominate; `risk_weight` was **not** part of this grid (it stays at B0's original 15, since B2's removal of it was never promoted). B7's full six-point threshold grid (TRAIN expectancy_r): 50&rarr;0.1169, with lower thresholds monotonically improving TRAIN expectancy at the cost of trade count -- reported in full in `data/calibration/entry_calibration_ladder.json`'s `B7_grid`, not just the winner.

## Final holdout evaluation (single look)

Frozen candidate: `technical_weight=100, momentum_weight=0, risk_weight=15 (additive, uncalibrated annualization), buy_threshold=50`.

| | Frozen candidate | B0 |
|---|---:|---:|
| HOLDOUT Spearman (composite vs. 5d return) | +0.0145 | +0.0203 |
| HOLDOUT trade count | 7,007 | 1,158 |
| HOLDOUT expectancy R | **+0.0248** | -0.0270 |
| HOLDOUT profit factor | 1.056 | 0.940 |
| HOLDOUT max drawdown (R) | 317.1 | 62.9 |
| HOLDOUT unique symbols | 40 | 36 |

**8-point promotion standard:**

1. Materially improved score ordering -- **FAIL** (holdout Spearman is actually 0.0058 *lower* for the frozen candidate than B0, the opposite of "improved")
2. Positive or improved holdout expectancy -- **PASS** (+0.0248 vs. -0.0270)
3. Acceptable drawdown -- **FAIL** (317.1R vs. 62.9R -- a 5x increase, driven mainly by the much larger trade count at the lower threshold; see caveat below)
4. Stability across chronological folds -- **PASS** (every promoted rung already cleared an independent TRAIN+VALIDATION check)
5. Broad symbol participation -- **PASS** (40 symbols, max single-symbol share 3.4%)
6. No dependence on one regime or short period -- not scored (holdout is a single, short, most-recent window by construction -- can't be regime-decomposed further without fabricating sub-samples)
7. Robustness to neighboring parameters -- **PASS** (full B6/B7 grids reported, not a single cherry-picked point)
8. Explainable deterministic semantics -- **PASS** (every rung is a documented, closed-form transform)

**Recommendation: `NO ENTRY-COMPOSITE CALIBRATION READY FOR PRODUCTION`.**

**Important caveat on the drawdown metric**: the max-drawdown-R figure here is the *sequential cumulative-R curve across independent, overlapping hypothetical trades* (same convention as the exit-parameter calibration's own `_metrics` helper) -- not a real, position-sized portfolio equity curve. It scales partly with trade *count*, so B7's much lower threshold (50 vs. 65) mechanically produces more trades and a larger cumulative-R drawdown even before asking whether any individual trade got worse. This is a real, legitimate reason for caution about the frozen candidate, not a methodology artifact to explain away -- but it should be read as "this candidate trades far more often, and the aggregate R-drawdown reflects that," not literally "the portfolio would have drawn down 317R."

**Bottom line**: exactly what the single-use-holdout discipline and non-regression guards were built to catch. B6/B7 looked like real improvements on TRAIN+VALIDATION (Spearman moved from -0.08 toward less-negative, expectancy roughly doubled) -- but the one-time HOLDOUT look shows the ordering improvement does not generalize (item 1 fails, marginally) and comes with a real cost in aggregate drawdown exposure (item 3 fails, substantially) from trading far more often at a lower threshold. Per the user's explicit rule, this holdout result is now terminal for this calibration generation -- it is not inspected further, and no parameter is adjusted and re-tested against it. **The current fixed composite (B0) remains the reference; no calibrated replacement is recommended for production.** A future attempt would need a new calibration generation with a genuinely untouched, later holdout period.

## Verification

- `python_tests/test_entry_calibration_ladder_harness.py`: 17/17 new tests pass (calendar re-annualization arithmetic, technical decomposition consistency, trailing-percentile momentum no-lookahead proof, gate direction filtering, pool-boundary non-overlap, and the structural proof that `evaluate_candidate`'s default pool set excludes HOLDOUT).
- Full existing suite: `.venv/bin/python -m pytest python_tests -q` -- green.
- `git diff --stat` / `git status`: zero changes under `tradepulse/` -- only new files under `tools/historical_data/`, `python_tests/`, and `docs/`.
- No commit, no push (per the user's explicit instruction for this phase).
