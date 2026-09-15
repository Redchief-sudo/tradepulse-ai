# Entry-composite cross-sectional out-of-universe check

**Read-only.** No `tradepulse/` file is changed by this pass -- no weight, threshold, or factor formula is touched in production. This is supplementary evidence, not a promotion decision -- it does not unfreeze Generation-2 (`docs/generation-2-calibration-freeze.md`) and does not run the ladder's formal TRAIN/VALIDATION/guard promotion process. Deterministic-layer-only, same permanent limitation as every prior document in this series: no AI-recommendation gate is simulated.

## Why this check exists

Rev.89's held-out calibration ladder (`docs/entry-composite-calibration-ladder.md`) concluded `NEW CALIBRATION GENERATION REQUIRED`: the corrected TRAIN/VALIDATION/HOLDOUT methodology selected a different frozen candidate (B6) than the original, already-holdout-viewed run, so HOLDOUT could not be touched again without contaminating it. A follow-up lineage audit (`docs/generation-2-calibration-freeze.md`) then proved every cached historical bar through **2026-09-04** has already been viewed by some tool in this series -- no temporal holdout remains until enough new market days accumulate strictly after that freeze date. As of this check, only ~4 calendar days had passed since the freeze -- far too little for a valid new temporal holdout.

This check asks a different, immediately-answerable question instead: does B6 hold up on symbols that **no script in this calibration lineage has ever loaded**? `fetch_alpaca_history.py`, `calibrate_exit_params.py`, `diagnose_signal_sparsity.py`, `entry_composite_audit.py`, and `entry_calibration_ladder.py` all iterate strictly over `tradepulse.strategy.universe.DEFAULT_EQUITY_UNIVERSE`/`DEFAULT_CRYPTO_UNIVERSE` (30 equities, 5 crypto pairs). A basket of symbols outside that list, even over already-viewed calendar dates, is genuinely unseen cross-sectional data -- a different, weaker axis of "out of sample" than a fresh future date, but real and available today rather than months away.

## Methodology

- **OOD universe** (verified programmatically disjoint from `DEFAULT_EQUITY_UNIVERSE`/`DEFAULT_CRYPTO_UNIVERSE` before any fetch): 18 equities spanning sectors absent or thin in the production universe -- tech (`NFLX, AMD, INTC, ORCL, CRM, ADBE, CSCO`), consumer (`DIS, PEP, COST, MCD`), financials (`GS, MS, WFC`), healthcare (`PFE, ABBV`), energy (`XOM, CVX` -- production has no energy name at all) -- plus 5 crypto pairs Alpaca confirmed bars for (`AVAX/USD, DOGE/USD, LINK/USD, UNI/USD, DOT/USD`).
- **Pipeline**: production's exact factor computation (`compute_real_factors`/`weighted_composite`/`signal_from_composite`) via `entry_calibration_ladder.py`'s own `generate_ladder_samples`, and its already-tested `evaluate_candidate` (25 passing tests) for Spearman/trade-metric/independence computation -- reused directly, not reimplemented. `pool_for_date` was monkeypatched to a single `"ood"` pool for the duration of this check only, since date-based TRAIN/VALIDATION/HOLDOUT partitioning is meaningless here -- every OOD sample is equally unseen regardless of its date; the axis of novelty is the symbol, not the date.
- **Candidates compared**: `B0` (current production baseline) vs. the ladder's frozen `B6` (`technical_weight=100, momentum_weight=0, risk_weight=15` additive/uncalibrated, `buy_threshold=65` -- copied verbatim from `data/calibration/entry_calibration_ladder.json`'s recorded rung, never re-derived by hand).
- **Reported per-asset-class as well as combined**, matching the original forensic audit's own discipline (pooling equity+crypto would let crypto's different sample volume mask what's actually happening in each class separately).
- New code: `tools/historical_data/out_of_universe_check.py`. Fetched bars cached under `data/calibration/{equity,crypto}/` via `fetch_alpaca_history.py`'s own `_fetch_and_cache` -- same cache format, new symbol filenames, never overwriting an in-universe file.

## Results

| | B0 Spearman (n) | B6 Spearman (n) | Δ Spearman | B0 expectancy_r | B6 expectancy_r | Δ expectancy_r | B0 trades | B6 trades |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Equity only** | -0.0026 (27,024) | **+0.0233** | **+0.0259** | +0.029 | +0.085 | +0.056 | 1,972 | 4,046 |
| **Crypto only** | -0.0163 (8,935) | -0.0117 | +0.0046 | -0.239 | +0.066 | +0.306 | **16** | 373 |
| Combined (for reference) | +0.0242 (35,959) | +0.0251 | +0.0009 | +0.026 | +0.083 | +0.057 | 1,988 | 4,419 |

Profit factor: equity B0 1.061 &rarr; B6 1.195; crypto B0 0.362 &rarr; B6 1.182. Max drawdown: equity/combined B0 96.8R &rarr; B6 114.6R (+18%, well short of the ladder's "more than doubled" guard threshold); crypto B0 5.0R &rarr; B6 41.8R (not comparable -- B0's crypto sample is 16 trades). Full independence metrics (unique symbols/dates, max single-symbol concentration) are in `data/calibration/out_of_universe_check.json`.

## Interpretation

**1. B0's defect looks less severe here than in-universe.** The original forensic audit found B0 clearly, robustly *negatively* correlated with forward returns everywhere it looked -- roughly -0.08 to -0.09 Spearman, both asset classes, every one of four walk-forward folds. On this fresh, never-touched basket, equity B0 is essentially flat (-0.003, indistinguishable from noise at this sample size), and only crypto still shows a negative sign, at roughly a fifth of the original magnitude. This does not overturn the original audit -- that finding was independently reproduced (Phase 2) and mechanistically explained (`risk_score`'s anti-correlation, Phase 4's interaction grid), and remains the reason B0 was frozen pending calibration. But it is a genuine, honestly-reported data point that the composite's directional defect may be more pronounced in the specific original 30-symbol universe (unusually top-heavy in mega-cap tech plus bond/gold ETFs) than as a universal property of the factor formulas across an arbitrary basket.

**2. B6 generalized better than B0 in both classes.** On OOD equities, B6's Spearman improvement over B0 (+0.026) clears the same +0.02 bar the ladder itself used as its TRAIN promotion threshold -- on symbols B6 was never tuned against. Expectancy and profit factor improve for B6 over B0 in both equity and crypto baskets. This is a real, if modest, sign that B6 (raising `technical_weight` to 100, zeroing `momentum_weight`, leaving `risk_score` additive and unweighted-change) is not narrowly overfit to the specific symbols it was selected on.

## Caveats and limitations

- **This is a cross-sectional check, not a temporal one.** It answers "does this generalize to new symbols," not "does this generalize to the future" -- the latter is what the Generation-2 freeze exists to protect, and remains unanswered until enough new market days accumulate strictly after 2026-09-04.
- **Crypto's B0 baseline is 16 trades.** `max_symbol_share` for B0's crypto trades is 0.5 -- one symbol accounts for half the sample. None of the crypto numbers here (for B0 especially) should be treated as reliable; they are reported for completeness, not as evidence.
- **No formal guard/promotion process was re-run.** This check reports raw deltas; it does not re-apply the ladder's non-regression guards (trade-count collapse, expectancy sign-flip, profit-factor floor, drawdown-doubling) as a pass/fail gate, because this is not a promotion step.
- **Single evaluation, no train/validation split within the OOD basket.** Unlike the ladder's disciplined TRAIN-selects/VALIDATION-confirms process, this is one evaluation over the whole OOD basket -- appropriate for a supplementary check, not sufficient on its own to select a production candidate.

## Bottom line

**No production change follows from this check**, consistent with every other document in this series. B0 remains the production reference; B6 remains an unpromoted candidate pending a genuine Generation-2 temporal holdout. What this check adds: (1) evidence that B0's directional defect, while real and independently reproduced in-universe, may not be as universally severe as first evidence suggested, and (2) evidence that B6 -- the one candidate the ladder's own TRAIN+VALIDATION process selected -- holds up directionally on a completely different set of symbols, which is a mildly encouraging sign for a future properly-run Generation-2 calibration rather than a reason to fast-track it now.

## Verification

- `tools/historical_data/out_of_universe_check.py`: programmatically asserts the OOD symbol list is disjoint from `DEFAULT_EQUITY_UNIVERSE`/`DEFAULT_CRYPTO_UNIVERSE` before any fetch, refusing to run otherwise.
- Reuses `entry_calibration_ladder.py`'s `evaluate_candidate` and `CandidateSpec` unmodified -- the same code already covered by `python_tests/test_entry_calibration_ladder_harness.py` (25/25 passing).
- `git status`: zero changes under `tradepulse/`; only new files under `tools/historical_data/`, `docs/`, and gitignored `data/calibration/`.
- No commit, no push.
