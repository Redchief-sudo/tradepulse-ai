# Deterministic entry-composite forensic audit

**Read-only.** This document is evidence for a decision, not the decision itself. No `tradepulse/` file is changed by this pass -- no weight, threshold, or factor formula is touched. Applying any conclusion below is a separate, later, explicitly-approved step. Per the same permanent limitation stated in every prior calibration document in this series: this analysis evaluates the **deterministic composite/exit layer in isolation** -- no AI-recommendation gate is simulated, because historical AI opinions cannot be reconstructed. Nothing here is a claim about historical performance of the full AI + deterministic TradePulse pipeline.

## Why this audit exists

Rev.86's exit-parameter calibration and crypto-sparsity diagnostic (`docs/exit-parameter-calibration.md`) found that the current deterministic composite does not order opportunities correctly: equity hypothetical expectancy peaks around the 50-59 composite band and turns negative through the live `>65` BUY region (65-69 &asymp; -0.009R, 70+ &asymp; -0.124R); crypto shows the same shape more sharply (45-49 &asymp; +0.152R vs 65-69 &asymp; -0.219R); no `break_even_trigger_pct`/`max_hold_days` combination could produce robust positive expectancy from the `>65` entry population. Because the deterioration appears in **both** asset classes, this is evidence about the composite/threshold design itself, not a crypto-only miscalibration. Per the user's explicit directive, the 60-day prove-edge clock does not start, and no weight/threshold is touched, until this is understood.

## Methodology

- **Phase 1** traces the exact source of every live factor and the composite/signal functions, answering ten fixed questions per factor about what it measures and whether its direction is coherent with "higher = stronger BUY."
- **Phase 2** independently reproduces Rev.86's composite-bucket findings against the same cached data, using the exact same primitives (`diagnose_signal_sparsity.py`'s `generate_daily_samples`/`_hypothetical_long_outcome`/`_aggregate_bucket_metrics`), and numerically diffs the result against the prior run's saved output.
- **Phase 3** measures whether each factor and the composite carry genuine, stable predictive ordering: Spearman rank correlation against forward returns, decile tables (return/MFE/MAE/hit rate) across five horizons, and stability across calendar folds, symbols, and market regimes.
- **Phase 4** checks whether individually weak/non-monotonic factors sharpen conditionally in 2D interaction with another factor.
- **Phase 5** classifies the failure mode(s) against the categories A-H, non-exclusively.
- **Phase 6** proposes (but does not execute) a held-out calibration methodology.

All computation reuses `tools/historical_data/`'s existing no-lookahead machinery and the same cached Alpaca history already fetched for Rev.86 (`data/calibration/{equity,crypto}/*.json`) -- no new data fetch was needed. New code: `tools/historical_data/entry_composite_audit.py` (Phases 2-4), `python_tests/test_entry_composite_audit_harness.py` (validation gate for this phase's new primitives: Spearman helper, decile bucketing, 2D interaction bucketing, regime as-of tagging).

---

## Phase 1: Factor semantics audit

Source: `tradepulse/strategy/factors.py` (`compute_real_factors`), `tradepulse/strategy/composite.py` (`weighted_composite`, `signal_from_composite`), `tradepulse/strategy/indicators.py` (raw formulas), `tradepulse/strategy/regime.py` (cited for contrast -- the one calendar-aware volatility computation in this codebase).

### `technical_score` (`factors.py:107-119`)

```
technical = 50.0
if rsi is not None:      technical += (50 - rsi) * 0.5        # range: [-25, +25]
if macd is not None:     technical += 10 if histogram > 0 else -10
if ma50 and ma200:       technical += 10 if ma50 > ma200 else -10
if bollinger is not None:
    if percent_b < 20:   technical += 8
    elif percent_b > 80: technical -= 8
technical = clamp(technical, 0, 100)
```

1. **Raw inputs**: RSI(14), MACD(12,26,9) histogram sign only (not magnitude), SMA50-vs-SMA200 cross, Bollinger %B(20, 2).
2. **Transformation**: baseline 50, plus a continuous RSI term (&plusmn;25 max) and three binary/discrete terms (&plusmn;10, &plusmn;10, &plusmn;8), clamped to [0,100].
3. **Is 100 explicitly "superior LONG opportunity"?** Not stated anywhere in source or docstring -- it is a mechanical blend with no documented design contract.
4-5. **What is it actually measuring?** Two opposed trading philosophies compressed into one number. The RSI term rewards **low** RSI (`(50-rsi)*0.5 > 0` when RSI < 50 -- mean-reversion/oversold logic) and the Bollinger term rewards being near the **lower** band (`percent_b < 20`, also mean-reversion). The MACD and MA50/MA200 terms reward **trend confirmation** (continuation). A stock in a strong, healthy uptrend with RSI > 70 (overbought) is *penalized* -10 (RSI term) and -8 (Bollinger term) by the same score that rewards its trend with +10/+10 (MACD/MA cross) -- these partially cancel. Conversely, a stock that merely bounced off oversold conditions with no confirmed trend scores well on 3 of 4 terms.
6. **Direction inverted relative to the composite's assumption?** Internally inconsistent rather than cleanly inverted -- `technical_score` doesn't mean "strong trend" or "good entry timing" on its own; it means some contradictory blend of the two, so a "higher = stronger BUY" reading of it is not well-founded on the formula alone. This is directly consistent with Phase 3/Rev.86 evidence of technical_score's own non-monotonicity (see below).
7. **Clipping/saturation:** the theoretical max above baseline is 25+10+10+8=53 (clipped to 100 only in extreme cases) -- clipping is not the primary driver of non-monotonicity; the internal RSI/Bollinger-vs-MACD/MA contradiction is.
8-9. **Equity-derived constants / asset-class behavior:** RSI/MACD/Bollinger are dimensionless, scale-invariant oscillators -- they don't inherit an equity-specific price-level assumption the way an ATR%-based threshold would. The MACD term's sign-only (magnitude-blind) treatment is untested across the very different per-bar move magnitudes of equity vs. crypto -- flagged as unverified, not concluded either way.
10. **Duplication with other factors:** conceptually overlaps with `momentum_score` (both derive from price trajectory) but via different transforms and windows -- correlated, not redundant.

### `momentum_score` (`factors.py:121`, `indicators.py:130-136`)

```
momentum_score = clamp(50 + momentum(closes, 14) * 2, 0, 100)
# momentum(14) = ((close[-1] - close[-1-14]) / close[-1-14]) * 100   -- raw 14-bar % change
```

1. **Raw input:** raw 14-bar percentage price change. Nothing else.
2. **Transformation:** linear, `50 + pct_change*2`, clamped [0,100] -- saturates once the 14-day move exceeds &plusmn;25%.
3-5. **Is 100 "opportunity quality"?** No explicit statement in source; it is literally "how much has price already risen in the last 14 days" -- a pure trailing-momentum measure, not a forward-looking quality assessment, and not a safety/risk measure either.
6. **Directional coherence:** monotonic in raw price change by construction, but whether "has already risen a lot in the last 14 days" should predict the *next* 5/10/15-day return positively is exactly what Phase 3 tests empirically -- momentum is well known in the general literature to sometimes continue and sometimes mean-revert depending on horizon and regime, so a naive "higher = more bullish forward signal" reading is a real assumption, not a proven fact, even before this audit's own data.
7. **Saturation:** genuinely consequential here, unlike `technical_score` -- crypto's 14-day swings routinely exceed &plusmn;25% (Rev.86's own distributions show crypto momentum_score p10=16.9/p90=90.9, roughly 3x wider than equity's p10=38.6/p90=64.3), meaning momentum_score saturates far more often for crypto, destroying ordering information in exactly the regime where the underlying signal swings hardest.
8-9. **Equity-derived constants:** the `*2` scaling constant has no calibration comment anywhere in `factors.py` (unlike the explicitly-calibrated ATR-quality thresholds a few lines away) -- it reads as a carried-over legacy constant with no per-asset-class distinction, despite the roughly 3x wider crypto distribution noted above.
10. **Duplication:** partially overlaps with `technical_score`'s trend-confirmation sub-terms (both are ultimately price-trajectory signals) but via an independent computation.

### `risk_score` (`factors.py:122`, `indicators.py:139-146`)

```
risk_score = clamp(100 - volatility(closes, 20), 0, 100)
# volatility(20) = stdev(20 daily returns) * sqrt(365) * 100   -- ALWAYS sqrt(365), no calendar parameter
```

1. **Raw input:** 20-day realized volatility of daily close-to-close returns.
2. **Transformation:** `100 - annualized_vol_pct`, clamped [0,100] -- any annualized vol &ge;100% floors the score at 0.
3-5. **Is 100 "superior LONG opportunity"?** Explicitly **not**, by the codebase's own account. `factors.py`'s own comment (lines 76-80) states `risk_quality_score` is "deliberately DISTINCT from `risk_score`... despite both being volatility-flavored" -- `risk_score` is a pure inverse-volatility/calmness measure, answering "how calm has this been," not "is this a good opportunity."
6. **Direction inverted relative to the composite's assumption -- the single clearest Category-A candidate:** summing a calmness measure (`risk_score`) with two opportunity/trend-strength measures (`technical_score`, `momentum_score`) into one number that `signal_from_composite` treats as a monotonic BUY-strength scale conflates two orthogonal, non-substitutable concepts. A genuinely strong trend/momentum setup is very often *more* volatile than average -- a breakout is, almost by construction, a volatility event -- so exactly the candidates `technical_score`/`momentum_score` are trying to reward get simultaneously punished by `risk_score`. Because `weighted_composite` is a weighted **average**, not a max, the highest-composite population is pulled toward candidates that are moderately good on all three factors simultaneously rather than genuinely strong on the ones that matter for direction -- which is directly consistent with Rev.86's finding that expectancy deteriorates specifically once the composite clears 65 in **both** asset classes.
7. **Saturation:** floors at 0 well before crypto's own regime.py-defined "crisis" volatility threshold (100% annualized) is reached -- crypto's regime.py `high_vol_threshold` is 80% annualized, meaning ordinary (non-crisis) crypto conditions can already floor this factor.
8-9. **Equity-derived constants -- a confirmed, code-level inconsistency, not a hypothesis:** `volatility()`'s `sqrt(365)` annualization is applied identically to equity and crypto. Contrast directly with `regime.py`, which explicitly calibrates separate `periods_per_year` (252 equity / 365 crypto) for the same kind of realized-vol computation, and with `factors.py`'s own `risk_quality_score` (ATR%-based), which uses calendar-aware tight/wide thresholds (`_ATR_QUALITY_THRESHOLDS`). `risk_score`'s `volatility()` call is the **one** volatility-flavored computation in this codebase that is not calendar-aware. Using `sqrt(365)` instead of `sqrt(252)` for equity also somewhat over-states equity's own annualized vol (a uniform bias, not something that changes the equity/crypto relative gap, but a real, fixable inconsistency in its own right).
10. **Duplication:** overlaps conceptually with `risk_quality_score` (both volatility-flavored) but computed differently (close-to-close stdev vs. ATR true-range) and, notably, `risk_quality_score` **is** calendar-aware while `risk_score` is not -- an inconsistency within the codebase's own factor set.

### `weighted_composite` (`composite.py:31-51`)

Renormalized weighted average over whichever factors have nonzero weight and a non-`None` score; under today's production weights (`technical_weight=25, momentum_weight=15, risk_weight=15`, all others 0) this reduces exactly to `(technical*25 + momentum*15 + risk*15) / 55`. `signal_from_composite`'s threshold cuts (`>80` STRONG_BUY ... `>65` BUY ... `<=30` STRONG_SELL) implicitly assume this weighted sum behaves as a single monotonic "opportunity strength" scale. **Nothing in `factors.py` or `composite.py` asserts or demonstrates that assumption** -- as shown above, `risk_score` is an explicitly different *kind* of measure (safety, not opportunity) than `technical_score`/`momentum_score`, and `technical_score` itself contains two internally opposed sub-philosophies. The composite sums them as if they were commensurate; the source gives no evidence they are.

### `signal_from_composite` (`composite.py:75-84`)

A fixed-threshold bucketing of the composite (30/45/65/80) into five labels, identical across asset classes. It inherits whatever defect the composite carries and adds one of its own: the thresholds are **universal**, with no asset-class distinction, despite the composite's own distribution differing sharply by class (Rev.86: equity BUY rate 6,516/52,682 = 12.4% vs. crypto 81/9,802 = 0.8%) -- the Category-D "universal threshold defect" hypothesis the user raised is visibly supported by this gap alone, before any of Phase 3/4's finer analysis.

### Phase 1 summary of candidate defects (to verify against Phases 2-4, not assumed)

| Candidate | Category | Evidence so far |
|---|---|---|
| `risk_score` measures safety, not opportunity strength, but is summed into a "higher=stronger BUY" composite | A | Explicit in source comments; mechanically dilutes exactly the high-momentum/high-technical candidates that are, by nature, more volatile |
| `technical_score` mixes mean-reversion (RSI, Bollinger) and trend-following (MACD, MA cross) sub-terms that partially cancel | A / F | Formula-level, confirmed by direct read |
| `volatility()`'s fixed `sqrt(365)` annualization, used only by `risk_score`, is not calendar-aware unlike every other volatility computation in this codebase | E | Formula-level, confirmed by direct read and contrast with `regime.py`/`risk_quality_score` |
| `momentum_score`'s fixed `*2` scaling saturates far more often for crypto's wider 14-day swings | E | Rev.86 distributions (crypto momentum p10=16.9/p90=90.9 vs. equity p10=38.6/p90=64.3) |
| `signal_from_composite`'s thresholds are universal across asset classes | D | Rev.86 BUY-rate gap (12.4% equity vs. 0.8% crypto) |

---

## Phase 2: Reproduction of Rev.86

Independently recomputed via `entry_composite_audit.py`'s `phase2_reproduce`, using the exact same primitives (`generate_daily_samples`, `_hypothetical_long_outcome`, `_bucket_for`, `_r_metrics`) against the same cached `data/calibration/{equity,crypto}/*.json` bar files -- **every composite bucket's expectancy_r matches the prior `sparsity_diagnostic.json` run to full floating-point precision, in both asset classes**:

| Composite | Equity (prior &rarr; reproduced) | Crypto (prior &rarr; reproduced) |
|---|---|---|
| <45 | 0.0649R &rarr; 0.0649R &#10003; | 0.1235R &rarr; 0.1235R &#10003; |
| 45-49 | 0.0792R &rarr; 0.0792R &#10003; | 0.1523R &rarr; 0.1523R &#10003; |
| 50-54 | 0.1170R &rarr; 0.1170R &#10003; | 0.0767R &rarr; 0.0767R &#10003; |
| 55-59 | 0.1150R &rarr; 0.1150R &#10003; | 0.0210R &rarr; 0.0210R &#10003; |
| 60-64 | 0.0308R &rarr; 0.0308R &#10003; | -0.0064R &rarr; -0.0064R &#10003; |
| 65-69 | -0.0090R &rarr; -0.0090R &#10003; | -0.2193R &rarr; -0.2193R &#10003; |
| 70+ | -0.1241R &rarr; -0.1241R &#10003; | 0.1957R &rarr; 0.1957R &#10003; |

Rev.86's finding is confirmed, not an artifact of a one-off run. Proceeding to Phases 3-4.

## Phase 3: Monotonicity / information-content analysis

### 3a. Spearman rank correlation, composite vs. forward return, by horizon

| Horizon | Equity (n&asymp;52.5k) | Crypto (n&asymp;9.8k) |
|---|---|---|
| 1d | -0.0135 | -0.0138 |
| 3d | -0.0235 | -0.0213 |
| 5d | -0.0354 | -0.0382 |
| 10d | -0.0551 | -0.0411 |
| 15d | -0.0567 | -0.0140 |

**The composite's rank correlation with forward return is negative at every horizon in both asset classes.** The magnitude is modest (weak-to-moderate by Spearman conventions), but it is measured across tens of thousands of samples and grows more negative with horizon through 10 days in both classes -- this is not sampling noise on a handful of extreme observations; it is present across the composite's *entire* distribution, not just the top bucket highlighted by the bucket tables above.

### 3b. Per-factor rank correlation (all six factors, not just the three live ones)

| Factor | Equity h1/h3/h5/h10/h15 | Crypto h1/h3/h5/h10/h15 |
|---|---|---|
| technical_score | +0.011 / +0.017 / +0.015 / +0.006 / +0.013 | +0.004 / -0.013 / -0.028 / -0.058 / -0.062 |
| momentum_score | -0.016 / -0.017 / -0.025 / -0.028 / -0.037 | -0.019 / -0.021 / -0.036 / -0.036 / +0.004 |
| **risk_score (live)** | **-0.030 / -0.056 / -0.064 / -0.083 / -0.091** | -0.002 / -0.001 / -0.003 / +0.015 / +0.030 |
| liquidity_score (zero-weighted) | +0.010 / +0.014 / +0.019 / +0.026 / +0.029 | +0.017 / +0.022 / +0.031 / +0.009 / -0.001 |
| **risk_quality_score (zero-weighted)** | **-0.029 / -0.054 / -0.063 / -0.083 / -0.089** | -0.011 / -0.009 / -0.014 / -0.005 / +0.008 |
| relative_strength_score (zero-weighted) | -0.007 / +0.003 / +0.001 / -0.003 / -0.004 | -0.026 / -0.034 / -0.048 / -0.059 / -0.045 |

**This is the single most important quantitative finding of the audit.** In equity, `risk_score` -- one of the three live, nonzero-weighted factors -- has the *largest-magnitude negative* correlation with forward return of any factor measured, growing to -0.091 at 15 days. That is not "orthogonal noise being averaged in"; it is a factor that is measurably **anti-correlated** with the outcome the composite is supposed to be selecting for, being added into the composite as though higher were better. Tellingly, `risk_quality_score` -- a **completely independently computed**, currently zero-weighted volatility-flavored factor (ATR%-based, not close-to-close-stdev-based) -- shows nearly the *identical* negative correlation profile (-0.029/-0.054/-0.063/-0.083/-0.089). Two differently-implemented "calmness" measures produce the same negative relationship with forward returns in this sample; this is evidence about calmness-as-a-signal generally in this dataset, not an artifact specific to `risk_score`'s particular formula (which independently also has the confirmed `sqrt(365)`-annualization defect from Phase 1). `technical_score` is weakly *positive* in equity but flips negative in crypto by 5+ days. `momentum_score` is negative in both classes -- the opposite of a naive "recent momentum continues" prior.

### 3c. Decile tables (composite, 5-day horizon)

| Decile (composite) | Equity avg 5d return / hit rate | Crypto avg 5d return / hit rate |
|---|---|---|
| 1 (lowest) | +0.30% / 53.5% | +1.03% / 54.6% |
| 2 | +0.23% / 53.6% | +0.91% / 53.4% |
| 3 | +0.35% / 55.2% | +0.63% / 51.0% |
| 4 | +0.51% / 57.1% | +0.67% / 51.5% |
| 5 | +0.36% / 55.5% | +0.57% / 49.1% |
| 6 | +0.35% / 56.1% | +2.84% / 51.5% (outlier-driven, see caveat below) |
| 7 | +0.15% / 55.1% | +1.38% / 48.9% |
| 8 | +0.07% / 53.5% | 0.00% / 46.4% |
| 9 | +0.12% / 53.0% | +0.48% / 49.7% |
| 10 (highest) | **-0.03% / 51.4%** | **-0.08% / 46.9%** |

Equity is close to monotonically declining past its decile-4 peak; hit rate alone (less sensitive to outlier magnitude than average return) declines almost monotonically from decile 5 (58.2%, see the `risk_score`-specific table below) through decile 10. Crypto's decile 6 average return is inflated by a small number of large-magnitude moves (crypto 5-day returns are fat-tailed; MFE at that decile is 11.3% vs. a neighborhood of 7-9%) -- noted as a caveat, not smoothed over -- but crypto's **hit rate** still declines close to monotonically from decile 1 (54.6%) to decile 10 (46.9%), and the bottom line (decile 10 negative on both metrics, in both asset classes) is unaffected by the decile-6 outlier.

`risk_score`'s own decile table (equity) makes the mechanism visible directly: hit rate declines from 58.2% (decile 5) to 48.1% (decile 10) -- the calmest quintile of observations has the *worst* subsequent hit rate of the whole distribution. `momentum_score`'s equity decile 1 (the lowest-momentum, i.e. worst recent performers) has the *best* forward outcome of the whole table (+1.03%, 60.4% hit rate) -- a mean-reversion pattern, the opposite of what a "higher momentum = more bullish" reading of the factor would predict.

### 3d. Year-by-year (fold) stability

Reusing the exact fold boundaries already approved for the exit-parameter calibration. Composite-vs-5-day-return Spearman correlation, by fold:

| Fold | Equity | Crypto |
|---|---|---|
| fold_1 (test 2023) | -0.0958 | -0.1037 |
| fold_2 (test 2024) | -0.0904 | -0.0603 |
| fold_3 (test 2025) | -0.0996 | -0.0869 |
| fold_4 (test 2025-09+) | -0.0140 | -0.0182 |

**Negative in every single fold, in both asset classes, across four non-overlapping walk-forward test windows spanning 2023 through the current partial period.** This directly answers the Phase 2 "insufficient evidence" concern (category H) in the negative -- the inversion is not a one-off artifact of a single period. The magnitude is largest in the earlier three folds and smallest (though still negative) in the short, partial, most-recent fold_4 window -- plausibly a smaller/noisier sample rather than the effect genuinely disappearing, but noted as an open question rather than asserted either way.

### 3e. Symbol-level stability

40 of the 41 universe symbols cleared the &ge;100-sample threshold (none were excluded as insufficient once the full history was used). **30 of 40 symbols (75%) show a negative composite-vs-5-day-return correlation**; only 10 show positive. The most negative correlations span both asset classes (XLI -0.122, XLV -0.102, XLE -0.098, IWM -0.098, LTC/USD -0.090), and the effect is not concentrated in a handful of outlier names -- it is the broad-based pattern across the universe, not a few symbols dragging an otherwise-clean aggregate.

### 3f. Regime-conditioned stability

Composite-vs-5-day-return Spearman correlation, tagged retroactively via `regime.classify_regime` (the same deterministic, calendar-aware classifier as the unwired regime-conditioned-weights experiment) on each sample's as-of benchmark history:

| Regime | Equity | Crypto |
|---|---|---|
| transition | +0.0061 | -0.0394 |
| range_bound_choppy | -0.0408 | -0.0272 |
| low_vol_bull | -0.0375 | +0.0316 |
| high_vol_bear | **-0.1834** | -0.0505 |
| liquidity_crisis | n/a (no equity samples classified) | **-0.6192** (n=60, thin -- see caveat) |

The inversion is **not uniform across regimes** -- it concentrates most sharply in `high_vol_bear` (equity: -0.18, more than 5x the all-regime average) and, in the one crypto period classified as `liquidity_crisis`, is dramatically negative (-0.62). Equity's `transition` regime is close to neutral (+0.006) and crypto's `low_vol_bull` is mildly positive (+0.03). This is mechanistically consistent with the Phase 1 finding: `risk_score`'s calmness reward is most actively wrong exactly when volatility is itself the most informative regime signal (bear/crisis), while in genuinely calm bull conditions the composite's ordering is roughly neutral. The `liquidity_crisis` cell (n=60) clears the 50-sample minimum-cell guard but is still a thin sample from what is, by definition, a rare regime -- read as directionally consistent with the bear-regime finding, not as a precise estimate.

## Phase 4: Interaction analysis

2D rank-tertile grids at the 5-day horizon (momentum&times;technical, technical&times;risk, momentum&times;risk). Composite&times;regime and composite&times;asset-class are not recomputed separately here -- they are exactly Phase 3e/3f's regime-stability and per-asset-class tables above, reused rather than duplicated.

**The clearest and most directly actionable interaction result, in both asset classes, is `technical_score` &times; `risk_score`:**

| Cell (equity) | avg 5d return | hit rate |
|---|---|---|
| high technical, **low** risk_score (volatile) | **+0.57%** | 54.7% |
| high technical, **high** risk_score (calm) | **0.00%** | **51.2%** (worst in the grid) |

| Cell (crypto) | avg 5d return | hit rate |
|---|---|---|
| low technical, low risk_score (volatile) | **+2.26%** | 50.4% |
| high technical, high risk_score (calm) | **-0.04%** | 49.5% |

In **both** asset classes, the cell the composite is mechanically designed to reward *most* -- strong technical setup **and** high risk_score (calm) -- is at or near the worst-performing cell in its own interaction grid, while pairing a strong technical setup with a **low** risk_score (i.e., volatile) is among the best-performing cells. This is concrete, cross-asset-class evidence that `risk_score`'s inclusion is not merely diluting the composite with orthogonal noise (which would be a milder Category C-only story) -- when combined with `technical_score` specifically, it is actively selecting against the higher-conviction technical setups, which are disproportionately the volatile ones. `momentum_score`&times;`risk_score` shows a similar, if noisier, pattern in both classes (the "high momentum, low risk_score" equity cell reaches +0.57%, the best in that grid; several "moderate momentum, high risk_score" crypto cells underperform their volatile counterparts).

`momentum_score`&times;`technical_score` shows a less clean pattern in both classes -- no cell dominates consistently, consistent with Phase 3's finding that `technical_score` and `momentum_score` are each only weakly, and inconsistently, predictive on their own; their interaction doesn't obviously sharpen either signal here.

## Phase 5: Failure classification

Evidence-supported, non-exclusive:

- **A -- Factor directionality defect: strongly supported.** `risk_score` (and the independently-computed, currently-unused `risk_quality_score`) is negatively correlated with forward returns at every horizon in equity, and the technical&times;risk interaction grid shows the composite's mechanically-favored "strong technical + calm" cell as the worst or near-worst performer in both asset classes. Summing a calmness measure into a "higher = stronger BUY" scale alongside trend/momentum measures is not merely imprecise -- the calmness measure actively works against the trend measures it's added to.
- **C -- Weighting/insufficient-information defect for risk_score, independently supported.** Rev.86 already showed `risk_score` has weak, non-monotonic within-crypto discrimination; this pass adds that it is actively anti-correlated in equity, and that the currently-zero-weighted `risk_quality_score` shows the same negative pattern -- reinforcing that this is a real property of "calmness as a signal" in this dataset, not an artifact of one formula's implementation.
- **E -- Asset-class normalization defect: confirmed at the source level (Phase 1), and circumstantially supported by data.** `volatility()`'s fixed `sqrt(365)` annualization (used only by `risk_score`) is the one volatility computation in this codebase not calendar-aware, unlike `regime.py` and `risk_quality_score`'s own ATR-based thresholds. `momentum_score`'s fixed `*2` scaling saturates far more often against crypto's wider swings (Rev.86 distributions). This doesn't fully explain risk_score's *directional* negativity (a calendar fix wouldn't flip a negative correlation to positive on its own), but it is a real, independent inconsistency worth fixing regardless of what else changes.
- **F -- Nonlinear interaction, weighted-sum inadequate: supported by Phase 4.** The technical&times;risk interaction is not well captured by a linear weighted average -- the *combination* of high technical and high risk_score is disproportionately bad in a way neither factor's own marginal (1D) correlation fully signals on its own (technical_score's own marginal correlation is near-zero-to-weakly-positive in equity; the damage only shows up in the 2D cell).
- **D -- Universal threshold defect: supported, but likely a downstream symptom rather than the root cause.** The equity/crypto BUY-rate gap (12.4% vs. 0.8%, from Rev.86) and the composite median gap are consistent with a threshold that isn't asset-class-aware, but Phase 3/4's evidence shows the underlying composite's *ordering* is inverted in equity too, at every threshold level, not just miscalibrated crypto-vs-equity in absolute level -- fixing the threshold alone would not fix the ordering problem.
- **B -- Universal-threshold-excludes-better-region: still supported by the bucket/decile shape (peak around the 45-59 range, declining through 65+), but subsumed by A/C/F above as the more fundamental explanation** -- moving the threshold down without fixing the underlying factor directionality/interaction issues would just relocate the same ordering problem to a new cutoff, not fix it (this is exactly the "critical constraint" the user's brief already anticipated).
- **G -- Insufficient predictive information: not supported as the primary story.** `technical_score` (equity) and `liquidity_score` (both classes, currently zero-weighted) show small but consistently signed positive correlations; the composite as assembled is actively counter-predictive, which is a stronger and more specific finding than "these factors just don't carry information."
- **H -- Insufficient evidence: not supported.** Phase 2 reproduced Rev.86 exactly; Phase 3's fold-stability table shows the core finding (negative composite-vs-forward-return correlation) holding across four independent walk-forward windows and 75% of individual symbols in both asset classes -- this is a well-corroborated pattern, not a thin or ambiguous one.

## Phase 6: Held-out calibration methodology (design only -- not executed)

**Not implemented or run in this pass**, per the user's explicit instruction. Proposed methodology for a later, separately-approved phase:

- **Folds**: reuse the exact chronological walk-forward boundaries already approved for the exit-parameter calibration (fold_1: train&le;2022-12-31/test 2023 ... fold_4: train&le;2025-08-31/test 2025-09+). The final fold (2025-09+) is held out completely -- never touched during any candidate-selection step, consistent with the user's "the final held-out period must remain completely untouched during parameter/model selection" instruction.
- **Candidate configurations to evaluate, only if each is independently justified by this audit's evidence** (not proposed wholesale):
  - Recalibrated/fixed `risk_score` annualization (calendar-aware `sqrt(252)`/`sqrt(365)`, matching `regime.py`'s and `risk_quality_score`'s existing convention) -- addresses the confirmed Category-E defect, testable in isolation from any directional/weighting change.
  - A `risk_score` directionality/weighting review, informed by Phase 3b/4's finding that it is anti-correlated with forward return, not merely low-information -- candidates include down-weighting it, using it as a filter/gate rather than an additive scale, or excluding it from the additive composite while retaining it as a separate risk-sizing input elsewhere.
  - A `technical_score` decomposition, given its internal mean-reversion/trend-following contradiction (Phase 1) and the technical&times;risk interaction finding (Phase 4) -- e.g. separating the RSI/Bollinger mean-reversion sub-terms from the MACD/MA-cross trend sub-terms into distinct, independently-weighted factors rather than one pre-blended number.
  - Explicitly **not** the previously-reverted regime-conditioned placeholder weights (per the user's own exclusion) -- though the regime-stability finding (Phase 3f: the inversion concentrates in `high_vol_bear`/`liquidity_crisis`) is relevant context for *any* future regime-aware design, it is not itself a proposal to revive that specific placeholder.
- **Evaluation metrics, all required, none sufficient alone**: expectancy R, profit factor, max drawdown, forward-return rank ordering (Spearman, this audit's own primitive), MFE/MAE, trade count, stability across folds/symbols/asset classes (this audit's own stability tables, applied to each candidate), sensitivity to neighboring parameter values, and concentration of performance (no small subset of symbols/dates driving the result) -- reusing `calibrate_exit_params.py`'s existing `_independence_metrics` concentration check as a direct precedent.
- **Mandatory baseline**: the current fixed composite (`technical=25, momentum=15, risk=15`) with the current universal `>65` BUY threshold, evaluated on the exact same folds/metrics -- any candidate must be compared against this baseline, not against the discovery-dataset numbers already shown above.
- **Selection discipline**: no configuration selected by total P&L alone; a configuration that wins on expectancy but fails the stability/concentration/sensitivity checks is not a valid candidate. The held-out final fold is used exactly once, to report the selected candidate's performance -- never to iterate.

## Bottom line

**The current deterministic composite should remain frozen pending calibration.** This audit found:

1. Phase 2 exactly reproduces Rev.86's finding -- not a fluke of one run.
2. Phase 3 shows the composite's rank correlation with forward return is **negative at every horizon, in both asset classes, in every one of four independent walk-forward folds, and in 75% of individual symbols** -- a broad-based, temporally stable pattern, not a thin or ambiguous one (ruling out category H).
3. The mechanism is identifiable, not just observed: `risk_score` (and the independently-implemented, currently-unused `risk_quality_score`) is itself negatively correlated with forward returns, and Phase 4's interaction analysis shows the exact combination the composite rewards most -- strong technical setup plus high risk_score/calmness -- is at or near the worst-performing cell in both asset classes. This is a directionality defect (Category A), not merely noise dilution.
4. A confirmed, independent implementation inconsistency (`risk_score`'s non-calendar-aware annualization, Category E) compounds the problem but is not its root cause -- fixing it alone would not resolve the directional finding above.
5. Lowering the BUY threshold, per the user's own stated constraint, would not fix this -- Phase 3's decile/fold tables show the inversion present across the *whole* composite distribution, not just above the current 65 cutoff; relocating the cutoff would relocate the same ordering problem, not correct it.

Starting the 60-day prove-edge clock on the current fixed composite would test a scoring hierarchy this audit found evidence is currently ordering candidates in the wrong direction at the high end, in a way that is temporally stable and broad-based across the universe. The recommended next step is the held-out calibration experiment designed in Phase 6, evaluated with the same rigor and held-out discipline as the exit-parameter calibration, before the 60-day period begins.
