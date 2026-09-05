# Deterministic entry-composite held-out calibration ladder

**Read-only.** No `tradepulse/` file is changed by this pass -- no weight, threshold, or factor formula is touched in production. This document reports calibration *evidence* and a *candidate recommendation*; applying it is a separate, later, explicitly-approved step. Deterministic-layer-only, same as every prior document in this series: no AI-recommendation gate is simulated, because historical AI opinions cannot be reconstructed -- nothing here is a claim about historical performance of the full AI + deterministic TradePulse pipeline.

## Why this phase exists

Rev.87's forensic audit confirmed Rev.86's finding and identified the mechanism: `risk_score` (a calmness measure) is negatively correlated with forward returns and, combined additively with `technical_score`/`momentum_score` into a "higher = stronger BUY" composite, actively rewards the wrong combination in both asset classes. The user approved this held-out calibration phase with an explicit **ablation ladder** (B0-B7) -- testing one narrow, isolated correction at a time rather than searching the whole parameter space simultaneously, which would very likely overfit the same discovery dataset that exposed the problem -- plus five additional controls: (1) the HOLDOUT pool is single-use for this calibration generation; (2) non-regression guards (expectancy R / profit factor / max drawdown / trade count) gate every promotion, not just Spearman; (3) `risk_score` is not forced to survive B3 if no gate variant clears the bar; (4) a B0 parity proof must pass before any calibration result is trusted; (5) TRAIN/VALIDATION/HOLDOUT isolation is enforced structurally in code and tested directly.

## Methodology correction (this revision)

The first run of this harness computed VALIDATION-pool metrics for every rung but never actually used them as a promotion gate -- only TRAIN decided promotion, with VALIDATION recorded alongside but not enforced. That is not the approved methodology (TRAIN selects, VALIDATION independently *confirms* before a rung is promoted, HOLDOUT is looked at exactly once at the very end). The user's own review caught this precisely, including a second, related gap: B7's ad hoc promotion check only enforced a trade-count floor, not the full non-regression guard suite (expectancy-flip / profit-factor / drawdown-doubling) the other rungs already used.

**Both are fixed in this revision**: `check_promotion` (Spearman-based rungs) and a new `check_promotion_by_expectancy` (B3/B7, whose candidates are selected by expectancy since a gate/threshold doesn't change the composite's ranking) now both require TRAIN to clear its bar **and** VALIDATION to independently confirm (Spearman or expectancy delta &ge; 0.0, plus the full guard suite) before returning `promoted=True`. The "stability across folds" item in the final promotion standard was also corrected -- it previously only checked that a promotion *record* existed (true even for a rejected rung, so it proved nothing); it now checks that every promoted rung actually carries recorded VALIDATION-confirmation evidence, which the corrected functions guarantee is present whenever `promoted=True`.

**Re-running the corrected methodology selected a DIFFERENT frozen candidate** than the original run -- B7 (lowering the BUY threshold from 65 to 50) is now **rejected**: its TRAIN max drawdown more than doubled (77.2R &rarr; 250.6R) against the full guard suite, a failure the original B7 logic's trade-count-only guard could not catch. The frozen candidate is now just B6 (technical=100/momentum=0/risk=15, **threshold unchanged at 65**), not B7's threshold=50.

Per the user's explicit instruction, **HOLDOUT was not re-evaluated for this new candidate** -- it was already viewed once, for the prior (methodologically-flawed) candidate, and that view must never be reused to tune or re-test a different one. The harness detects this automatically: it compares the corrected run's frozen candidate against the prior run's recorded candidate, and only reuses/reports HOLDOUT numbers when they are identical (structural safeguard: `test_spec_to_dict_is_stable_for_identical_specs_and_differs_for_different_ones`). Here they are not identical, so the script halts before touching HOLDOUT and reports `NEW CALIBRATION GENERATION REQUIRED` instead of a holdout verdict. See "Final evaluation" below for what this means in practice -- it does **not** change the production recommendation.

## Methodology

- **Pools** (non-overlapping, reusing the same chronological cutoffs already approved for the exit-parameter calibration): TRAIN = 2023-01-01 to 2024-12-31; VALIDATION = 2025-01-01 to 2025-08-31; HOLDOUT = 2025-09-01 onward. HOLDOUT is loaded only once, in the final step -- never passed into any selection/promotion function (structurally proven in `test_entry_calibration_ladder_harness.py::test_evaluate_candidate_default_pools_exclude_holdout`).
- **Promotion rule** (primary, corrected): TRAIN-pool composite-vs-5-day-forward-return Spearman must improve by &ge;+0.02 absolute vs. the current frozen rung AND pass TRAIN guards; VALIDATION must then independently show a non-negative delta AND pass VALIDATION guards before the rung is promoted. VALIDATION is used only to confirm or veto a TRAIN-selected candidate, never to search for a better one.
- **Non-regression guards** (pass/fail, never optimization targets, applied to BOTH pools): a rung is not promoted if, vs. the prior frozen rung, trade count collapses below 20% of its prior value, expectancy R flips from positive to negative, profit factor drops below 0.5, or max drawdown more than doubles.
- **B3/B7 exception**: threshold- and gate-defining rungs are selected by TRAIN expectancy R, not Spearman (a threshold/gate doesn't change the composite's ranking, only which points are labeled "enter" -- Spearman is identical across every threshold candidate and cannot distinguish between them) -- but still require VALIDATION expectancy confirmation before promotion, via the same two-pool discipline.
- All new primitives (raw sub-indicator extraction, the Spearman/decile helpers, the pool partition, the gate/decomposition/normalization candidates, and the corrected two-pool promotion rules) are covered by `python_tests/test_entry_calibration_ladder_harness.py` (25 tests) and reuse `tools/historical_data/entry_composite_audit.py`/`simulate_trades.py`/`diagnose_signal_sparsity.py`'s existing primitives wherever the computation is identical to what those scripts already do.

## B0 parity proof

**Passed.** The generalized ladder harness's B0 candidate (built through the same new entry/exit pipeline every other rung uses) reproduces `diagnose_signal_sparsity.generate_daily_samples`'s existing, already-verified composite/signal output **exactly** -- 62,484 samples checked, 0 mismatches. This is a structural proof, not a spot check: every sample's `composite`/`signal` was compared field-by-field. Per the mandated control, B1-B7 selection only proceeded after this passed.

## Ladder results

Pools: TRAIN = 2023-2024 (n&asymp;20,807 samples with a valid 5-day forward return), VALIDATION = Jan-Aug 2025 (n&asymp;6,990), HOLDOUT = Sep 2025+ (untouched until the final step).

| Rung | Change | TRAIN &Delta; | VALIDATION &Delta; | Promoted? | Reason |
|---|---|---:|---:|---|---|
| B0 | baseline | -- | -- | -- | mandatory baseline (TRAIN Spearman -0.0841, VALIDATION -0.0942) |
| B1 | calendar-aware risk_score annualization | +0.0060 | not reached | **No** | TRAIN Spearman below the +0.02 threshold |
| B2 | drop risk_score from the composite | +0.0332 | not reached | **No** | TRAIN Spearman cleared the bar, but the TRAIN guard caught it: expectancy_r flipped **positive to negative** (+0.0052 &rarr; -0.1010) -- rejected before VALIDATION is even checked |
| B3 | risk_score as a gate | -- | -- | **Skipped** | B2 wasn't promoted, so per the plan's explicit rule B3 is not evaluated; risk_score remains additive, unchanged from B0 |
| B4 | technical decomposition (mean_reversion / trend_confirmation / avg_decomposed) | +0.0179 (best: mean_reversion) | not reached | **No** | TRAIN Spearman below the +0.02 threshold |
| B5 | momentum normalization (linear_x2 / calendar_rescaled / percentile_rank) | +0.0008 (best: percentile_rank) | not reached | **No** | below threshold, and its own TRAIN guards also failed (expectancy flip; drawdown more than doubled) |
| B6 | technical/momentum weight grid (11 points, risk_weight held at 15) | **+0.0450** (winner: technical=100, momentum=0) | **+0.0273** | **Yes** | TRAIN clears, VALIDATION independently confirms, both guard sets pass |
| B7 | BUY-threshold grid (50/55/60/65/70/75), selected by TRAIN expectancy_r | expectancy +0.0330 | not reached | **No** | TRAIN expectancy improved (0.084&rarr;0.117), but the full TRAIN guard suite (not just a trade-count floor, per the correction below) caught it: max drawdown more than doubled (77.2R &rarr; 250.6R) |

**B2 and B7 are the two most important intermediate results in this run.** B2 is the one candidate that would have looked attractive by the ordering metric alone -- caught by the TRAIN guard. B7 is the one the *original, uncorrected* implementation actually promoted, because its ad hoc check only enforced a trade-count floor -- the full guard suite (applied consistently for the first time in this corrected run) catches the drawdown blow-up that a threshold drop from 65 to 50 causes (trade count 4,174 &rarr; 14,661). Both are direct, concrete validation of "guards on every rung, not just some."

B6's winning grid point is `technical_weight=100, momentum_weight=0` -- momentum contributed nothing once technical was allowed to dominate; `risk_weight` was **not** part of this grid (stays at B0's original 15, since B2's removal of it was never promoted). B7's full six-point threshold grid and B4/B5's full candidate sets are reported in `data/calibration/entry_calibration_ladder.json`, not just the winners.

## Final evaluation: a new calibration generation is required, not a holdout failure

**The frozen candidate under the corrected methodology is just B6**: `technical_weight=100, momentum_weight=0, risk_weight=15 (additive, uncalibrated annualization), buy_threshold=65` (unchanged from B0's threshold, since B7 is now rejected).

This is a **different candidate** than the prior (methodologically-flawed) run's frozen candidate, which included B7's threshold=50. The harness's structural safeguard compared the two specs, found them different, and **halted before touching HOLDOUT** -- per the user's explicit rule, a HOLDOUT pool that was already viewed once (for the prior candidate) must never be reused to evaluate a different one; that would make it validation data in disguise. The correct, honest outcome is:

> **`NEW CALIBRATION GENERATION REQUIRED`** -- the corrected methodology's actual candidate (B6 only, threshold=65) has never been evaluated against any untouched holdout, and must not be until a genuinely new, later holdout period exists.

**This does not change the production recommendation.** Whether or not B6-alone would have passed a holdout look is now, honestly, unknown -- and per the user's own rule, that is not a gap to paper over by reusing the already-viewed holdout. B0 (the current fixed production composite) remains the reference either way, since nothing in this calibration generation produced a holdout-validated replacement for it.

For historical/audit context only -- **not a valid evaluation of B6, and not to be read as evidence about it** -- the *prior* run's already-viewed holdout numbers (for the different, B7-threshold=50 candidate) were: HOLDOUT Spearman +0.0145 vs. B0's +0.0203 (worse ordering), HOLDOUT expectancy +0.0248 vs. B0's -0.0270 (better expectancy), HOLDOUT max drawdown 317.1R vs. B0's 62.9R (substantially worse, driven by trade count at the lower threshold). That candidate is superseded by this correction and is not being proposed.

**Bottom line**: the single-use-holdout discipline and non-regression guards did exactly what they were built to do -- caught a methodology gap (missing VALIDATION gate, incomplete B7 guards) before it could produce a false institutional conclusion. **The current fixed composite (B0) remains the reference; no calibrated replacement is recommended for production.** A future attempt at calibrating B6-alone (or anything else) requires a new calibration generation with a genuinely untouched, later holdout period -- not this one.

## Verification

- `python_tests/test_entry_calibration_ladder_harness.py`: 25/25 tests pass (17 from the original pass, plus 8 new: VALIDATION-must-independently-confirm for both `check_promotion` and `check_promotion_by_expectancy`, a guard-failure-blocks-promotion-regardless-of-Spearman case, a minimum-VALIDATION-trade-count guard, and the spec-equality check the holdout-reuse safeguard depends on).
- Full existing suite: `.venv/bin/python -m pytest python_tests -q` -- green.
- `git diff --stat` / `git status`: zero changes under `tradepulse/` -- only new files under `tools/historical_data/`, `python_tests/`, and `docs/`.
- No commit, no push (per the user's explicit instruction for this phase).
