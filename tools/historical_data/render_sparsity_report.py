"""Renders the crypto-signal-sparsity diagnostic (data/calibration/
sparsity_diagnostic.json) into a markdown section, printed to stdout so it
can be reviewed before appending to docs/exit-parameter-calibration.md.
Generated programmatically rather than hand-transcribed, to avoid
transcription errors across the volume of numbers involved.
"""

import json
import sys
from pathlib import Path

CACHE_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
D = json.loads((CACHE_ROOT / "sparsity_diagnostic.json").read_text())

HORIZONS = [1, 3, 5, 10, 15]


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x*100:.2f}%"


def num(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


out = []
w = out.append

w("## Finding: crypto fixed-composite signal sparsity\n")
w(
    "Discovered while reviewing the exit-parameter calibration's entry counts: BCH/USD's composite "
    "score crossed the production BUY threshold (>65) exactly once across 2,044 gap-free days of "
    "history. This section is a **separate, read-only diagnostic** -- it does not calibrate or change "
    "`break_even_trigger_pct`/`max_hold_days` (that's the section above), and it changes no production "
    "weight, threshold, or factor formula. It only produces evidence for a later, separately-approved "
    "decision.\n"
)
w(
    "**Grounding fact**: under the current fixed baseline weights (`default_strategy_weights()`), "
    "`liquidity_weight`/`risk_quality_weight`/`relative_strength_weight` are all zero -- "
    "`weighted_composite`'s renormalization excludes a zero-weight factor entirely. So today, only "
    "**technical_score/momentum_score/risk_score** (weights 25/15/15) actually drive the live composite; "
    "the other three factors are computed but currently inert.\n"
)

w("### Per-symbol evidence\n")
w("| Symbol | Class | Bars | Gaps | Median | p90 | Count >65 | % >65 |")
w("|---|---|---|---|---|---|---|---|")
for sym, s in sorted(D["symbol_summary"].items(), key=lambda kv: (kv[1]["asset_class"] or "", kv[0])):
    w(f"| {sym} | {s['asset_class']} | {s['bar_count']} | {s['gap_count']} | {num(s.get('median'),1)} | {num(s.get('p90'),1)} | {s['count_above_65']} | {pct(s.get('pct_above_65'))} |")
w("")
w(
    "SOL/USD's shorter coverage and gap count (visible above) is a genuine, isolated data-completeness "
    "issue -- noted separately, not generalized to the rest of the crypto universe (BTC/ETH/LTC/BCH all "
    "show 0 gaps and ~2,044 gap-free days).\n"
)

w("### Distributions: equity vs. crypto, all six factors + composite\n")
for ac in ("equity", "crypto"):
    w(f"**{ac}** (n={D['distributions'][ac]['total']})\n")
    w("| Factor | p10 | p25 | median | p75 | p90 |")
    w("|---|---|---|---|---|---|")
    for factor in ("technical_score", "momentum_score", "risk_score", "liquidity_score", "risk_quality_score", "relative_strength_score", "composite"):
        stats = D["distributions"][ac][factor]
        w(f"| {factor} | {num(stats.get('p10'),1)} | {num(stats.get('p25'),1)} | {num(stats.get('median'),1)} | {num(stats.get('p75'),1)} | {num(stats.get('p90'),1)} |")
    w(f"\nSignal counts: {D['distributions'][ac]['signal_counts']}\n")

w(
    "**The gap is concentrated almost entirely in `risk_score`**: technical_score's median is nearly "
    "identical between classes (equity 50.8 vs crypto 49.6), as is momentum_score's (51.1 vs 49.5, though "
    "crypto's momentum spread is far wider in both directions). risk_score's median is 77.8 (equity) vs "
    "34.8 (crypto) -- crypto's own p90 (64.2) barely reaches equity's p10 (54.8). Weighted through the "
    "composite (technical*25 + momentum*15 + risk*15, over 55), risk_score's contribution alone "
    "(~21.2 for equity vs ~9.5 for crypto, a ~11.7-point gap) accounts for essentially the entire observed "
    "composite median gap (58.1 vs 46.4, an 11.7-point gap). Per-symbol evidence confirms this is a "
    "volatility effect, not an asset-class label: TSLA (equity, high realized volatility) has a median "
    "composite of 47.1 -- indistinguishable from crypto -- while SHY (equity, a low-volatility bond ETF) "
    "has the highest median of any universe symbol (63.3) and by far the most BUY signals (34.3% of days). "
    "`risk_score` is functioning as a realized-volatility detector that happens to disadvantage crypto "
    "systematically, since crypto is structurally higher-volatility than nearly every equity in the "
    "universe -- not something that inspects asset class directly.\n"
)

w("### Threshold sensitivity (diagnostic only -- not a proposal)\n")
w("| Threshold | Equity % crossing | Crypto % crossing |")
w("|---|---|---|")
for t in ("55", "60", "65", "70"):
    w(f"| >{t} | {pct(D['threshold_sensitivity']['equity'][t])} | {pct(D['threshold_sensitivity']['crypto'][t])} |")
w("")

w("### 4a. Hypothetical long outcome conditional on composite score\n")
w(
    "**Exit-policy-dependent.** Every daily observation, regardless of signal, simulated as a hypothetical "
    "long using `_atr_stop_loss_price`/`_stop_loss_price` for the entry stop and `simulate_exit` at one "
    "fixed, already-in-production parameter set (balanced profile: break_even_trigger_pct=4, "
    "max_hold_days=15, trailing_atr_multiplier=2.5 -- decoupled from the separate exit-parameter grid "
    "sweep, not re-optimized here). **This is not production expectancy or historical trades** -- "
    "production never entered on sub-threshold observations, and these samples are heavily overlapping "
    "(same-symbol adjacent-day observations share most of their holding period).\n"
)
for ac in ("equity", "crypto"):
    w(f"**{ac}**\n")
    w("| Composite | n | Censored | Unique symbols | Unique dates | Hit rate | Expectancy (R) | Profit factor |")
    w("|---|---|---|---|---|---|---|---|")
    for bucket, m in D["bucketed"]["by_composite"][ac]["hypothetical_long_outcome"].items():
        w(f"| {bucket} | {m['sample_count']} | {m['censored']} | {m['unique_symbols']} | {m['unique_dates']} | {pct(m.get('hit_rate'))} | {num(m.get('expectancy_r'))} | {num(m.get('profit_factor'),2)} |")
    w("")

w("### 4b. Composite-score predictive discrimination, independent of the exit policy\n")
w(
    "**Exit-policy-independent** -- pure forward return/MFE/MAE from closes/highs/lows only, no stop, no "
    "time-stop, no exit simulation. This is what separates \"does the score predict direction\" from "
    "\"does today's exit system monetize it.\"\n"
)
for ac in ("equity", "crypto"):
    w(f"**{ac}** -- 5-day and 15-day forward return, by composite bucket\n")
    w("| Composite | n | Unique symbols | 5d return | 5d MFE | 5d MAE | 15d return |")
    w("|---|---|---|---|---|---|---|")
    for bucket, m in D["bucketed"]["by_composite"][ac]["predictive_discrimination"].items():
        h5, h15 = m.get("h5", {}), m.get("h15", {})
        w(f"| {bucket} | {m['sample_count']} | {m['unique_symbols']} | {pct(h5.get('avg_return'))} | {pct(h5.get('avg_mfe'))} | {pct(h5.get('avg_mae'))} | {pct(h15.get('avg_return'))} |")
    w("")

w("### 4c. Per-factor predictive discrimination (technical/momentum/risk -- the three live factors)\n")
for factor in ("risk_score", "technical_score", "momentum_score"):
    w(f"**{factor}**\n")
    for ac in ("equity", "crypto"):
        w(f"*{ac}*\n")
        w("| Bucket | n | Unique symbols | 5d return | 15d return |")
        w("|---|---|---|---|---|")
        for bucket, m in D["bucketed"]["by_factor"][factor][ac].items():
            h5, h15 = m.get("h5", {}), m.get("h15", {})
            w(f"| {bucket} | {m['sample_count']} | {m['unique_symbols']} | {pct(h5.get('avg_return'))} | {pct(h15.get('avg_return'))} |")
        w("")

print("\n".join(out))
