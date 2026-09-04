"""Read-only forensic audit of the deterministic entry composite (Phases
2-4 of the entry-composite forensic audit -- see
docs/entry-composite-forensic-audit.md for the full report, including
Phase 1's source-level factor-semantics analysis and Phases 5/6's written
classification/methodology, which are judgment calls made from this
script's numbers, not computed here).

Rev.86's exit-parameter calibration and crypto-sparsity diagnostic found
that higher `weighted_composite` scores do NOT correspond to better
forward outcomes, in both equity and crypto. This script:

  Phase 2 -- independently reproduces that finding against the same cached
  data, to confirm it before anything else is investigated.
  Phase 3 -- measures whether each individual factor (and the composite)
  carries genuine, stable predictive ordering (Spearman rank correlation,
  decile tables, year/symbol/regime stability).
  Phase 4 -- checks whether individually weak/non-monotonic factors sharpen
  conditionally in combination with another factor.

No tradepulse/ file is touched; no weight/threshold/formula is changed.
Reuses diagnose_signal_sparsity.py's no-lookahead daily-sample generator
and forward-metric primitives directly rather than re-deriving them, and
simulate_trades.py's exit simulation (only for the Phase 2 reproduction,
which is the one part of this analysis that is exit-policy-dependent --
everything in Phases 3/4 uses exit-policy-INDEPENDENT forward
return/MFE/MAE, matching the discipline the user asked for: separate
"does score predict returns" from "does exit policy monetize it").

Deterministic-layer-only, same as every other tool in this directory: no
AI-recommendation gate is simulated, so nothing here is a claim about
historical performance of the full AI + deterministic pipeline -- only
about the deterministic composite/exit layer in isolation.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOL_DIR))

from tradepulse.strategy.regime import Calendar, Regime, classify_regime  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
from calibrate_exit_params import FOLDS  # noqa: E402
from diagnose_signal_sparsity import (  # noqa: E402
    COMPOSITE_BUCKETS, HORIZONS, Sample, _aggregate_bucket_metrics, _bucket_for,
    _forward_metrics, _hypothetical_long_outcome, _r_metrics, generate_daily_samples,
)
from simulate_trades import CACHE_ROOT, _benchmark_closes_as_of  # noqa: E402

RESULTS_PATH = CACHE_ROOT / "entry_composite_audit.json"
SPARSITY_DIAGNOSTIC_PATH = CACHE_ROOT / "sparsity_diagnostic.json"

FACTOR_NAMES = (
    "technical_score", "momentum_score", "risk_score", "liquidity_score", "risk_quality_score",
    "relative_strength_score", "composite",
)

MIN_SYMBOL_SAMPLES = 100  # below this, a per-symbol correlation is reported as insufficient, not computed
MIN_REGIME_CELL = 50  # below this, a regime cell is reported as insufficient, not computed
MIN_INTERACTION_CELL = 20  # below this, a 2D interaction cell is reported as insufficient, not computed
INTERACTION_HORIZON = 5  # the same horizon used throughout for the interaction grids (5-day forward return)
STABILITY_HORIZON = 5  # the horizon used for the year/symbol/regime stability summaries


def _factor_value(sample: Sample, factor: str) -> float | None:
    value = getattr(sample, factor)
    return None if value is None else float(value)


# ---- Phase 3a: Spearman rank correlation (dependency-free, average-rank ties) --------------------


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed average rank across the tied block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rho == Pearson correlation of the two rank series. None
    (not 0.0) when there's too little data or either series is constant --
    a genuinely undefined correlation must never be reported as "no
    relationship" (0.0 has a different, false meaning)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    den_x = sum((a - mean_rx) ** 2 for a in rx) ** 0.5
    den_y = sum((b - mean_ry) ** 2 for b in ry) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def correlation_table(records: list[tuple[Sample, dict]], factor: str) -> dict:
    out: dict = {}
    for horizon in HORIZONS:
        xs: list[float] = []
        ys: list[float] = []
        for sample, fm in records:
            val = _factor_value(sample, factor)
            if val is None or horizon not in fm:
                continue
            xs.append(val)
            ys.append(fm[horizon]["return"])
        out[f"h{horizon}"] = {"n": len(xs), "spearman": spearman(xs, ys)}
    return out


# ---- Phase 3b: decile tables (rank-based, robust to duplicate score mass) ------------------------


def decile_table(records: list[tuple[Sample, dict]], factor: str) -> dict:
    usable = [(v, fm) for s, fm in records if (v := _factor_value(s, factor)) is not None]
    usable.sort(key=lambda t: t[0])
    n = len(usable)
    if n == 0:
        return {}
    bucket_rows: dict[int, list[dict]] = {d: [] for d in range(10)}
    for idx, (_val, fm) in enumerate(usable):
        bucket_rows[min(9, (idx * 10) // n)].append(fm)
    out: dict = {}
    for d in range(10):
        rows = bucket_rows[d]
        if not rows:
            continue
        entry: dict = {"n": len(rows)}
        for horizon in HORIZONS:
            vals = [r[horizon] for r in rows if horizon in r]
            if not vals:
                continue
            entry[f"h{horizon}"] = {
                "n": len(vals),
                "avg_return": sum(v["return"] for v in vals) / len(vals),
                "avg_mfe": sum(v["mfe"] for v in vals) / len(vals),
                "avg_mae": sum(v["mae"] for v in vals) / len(vals),
                "hit_rate": sum(1 for v in vals if v["return"] > 0) / len(vals),
            }
        out[f"decile_{d + 1}"] = entry
    return out


# ---- Phase 3c/3d: year (fold) and symbol stability ------------------------------------------------


def _composite_bucket_summary(records: list[tuple[Sample, dict]]) -> dict:
    by_bucket: dict[str, list[tuple[Sample, dict]]] = {name: [] for name, _lo, _hi in COMPOSITE_BUCKETS}
    for sample, fm in records:
        bucket = _bucket_for(sample.composite, COMPOSITE_BUCKETS)
        if bucket is None:
            continue
        by_bucket[bucket].append((sample, fm))
    return {b: _aggregate_bucket_metrics(v) for b, v in by_bucket.items()}


def fold_stability(flat: dict[str, list[tuple[Sample, dict]]]) -> dict:
    out: dict = {}
    for asset_class, records in flat.items():
        out[asset_class] = {}
        for fold in FOLDS:
            windowed = [
                (s, fm) for s, fm in records
                if fold["test_start"] <= date.fromisoformat(s.date) <= fold["test_end"]
            ]
            out[asset_class][fold["name"]] = {
                "sample_count": len(windowed),
                "composite_vs_h5_spearman": correlation_table(windowed, "composite")[f"h{STABILITY_HORIZON}"],
                "composite_bucket_forward_returns": _composite_bucket_summary(windowed),
            }
    return out


def symbol_stability(all_samples: dict[str, tuple[list[Sample], list[dict]]], forward_by_symbol: dict[str, list[tuple[Sample, dict]]]) -> dict:
    out: dict = {}
    for symbol, records in forward_by_symbol.items():
        if len(records) < MIN_SYMBOL_SAMPLES:
            out[symbol] = {"sample_count": len(records), "insufficient_sample": True}
            continue
        out[symbol] = {
            "sample_count": len(records),
            "composite_vs_h5_spearman": correlation_table(records, "composite")[f"h{STABILITY_HORIZON}"],
        }
    return out


# ---- Phase 3e: regime-conditioned stability --------------------------------------------------------


def _regime_cache_key(asset_class: str, as_of_date: str) -> tuple[str, str]:
    return (asset_class, as_of_date)


def tag_regimes(
    flat: dict[str, list[tuple[Sample, dict]]], benchmark_bars_by_class: dict[str, list[dict]],
) -> dict[str, list[tuple[Sample, dict, str]]]:
    """Retroactively classifies each sample's as-of date with the same
    deterministic, calendar-aware regime classifier already used for the
    (unwired) regime-conditioned weights experiment -- reads only the
    benchmark's own closes up to and including the sample's date, the same
    as-of discipline generate_daily_samples already uses for
    relative_strength_score. Cached by (asset_class, date) since many
    samples across different symbols share the same as-of date and the
    classifier only depends on the benchmark, never the symbol."""
    cache: dict[tuple[str, str], Regime] = {}
    out: dict[str, list[tuple[Sample, dict, str]]] = {}
    for asset_class, records in flat.items():
        calendar: Calendar = "crypto" if asset_class == "crypto" else "equity"
        bench_bars = benchmark_bars_by_class[asset_class]
        tagged: list[tuple[Sample, dict, str]] = []
        for sample, fm in records:
            key = _regime_cache_key(asset_class, sample.date)
            if key not in cache:
                closes = _benchmark_closes_as_of(bench_bars, sample.date)
                cache[key] = classify_regime(closes, calendar=calendar).regime
            tagged.append((sample, fm, cache[key]))
        out[asset_class] = tagged
    return out


def regime_stability(tagged: dict[str, list[tuple[Sample, dict, str]]]) -> dict:
    out: dict = {}
    for asset_class, records in tagged.items():
        by_regime: dict[str, list[tuple[Sample, dict]]] = {}
        for sample, fm, regime in records:
            by_regime.setdefault(regime, []).append((sample, fm))
        out[asset_class] = {}
        for regime, regime_records in by_regime.items():
            if len(regime_records) < MIN_REGIME_CELL:
                out[asset_class][regime] = {"sample_count": len(regime_records), "insufficient_sample": True}
                continue
            out[asset_class][regime] = {
                "sample_count": len(regime_records),
                "composite_vs_h5_spearman": correlation_table(regime_records, "composite")[f"h{STABILITY_HORIZON}"],
                "composite_bucket_forward_returns": _composite_bucket_summary(regime_records),
            }
    return out


# ---- Phase 4: 2D interaction analysis ---------------------------------------------------------------


def interaction_table(records: list[tuple[Sample, dict]], factor_x: str, factor_y: str) -> dict:
    """Rank-based tertiles on each axis independently (never a raw-value
    cut, which duplicate-heavy factor distributions would skew), sample-
    count-guarded per cell so a thin cell reads as insufficient rather than
    a misleadingly precise average."""
    usable = [
        (vx, vy, fm[INTERACTION_HORIZON])
        for s, fm in records
        if INTERACTION_HORIZON in fm
        and (vx := _factor_value(s, factor_x)) is not None
        and (vy := _factor_value(s, factor_y)) is not None
    ]
    n = len(usable)
    if n == 0:
        return {}
    order_x = sorted(range(n), key=lambda i: usable[i][0])
    order_y = sorted(range(n), key=lambda i: usable[i][1])
    rank_x = [0] * n
    rank_y = [0] * n
    for pos, i in enumerate(order_x):
        rank_x[i] = pos
    for pos, i in enumerate(order_y):
        rank_y[i] = pos

    cells: dict[str, list[dict]] = {}
    for i in range(n):
        tx = min(2, (rank_x[i] * 3) // n)
        ty = min(2, (rank_y[i] * 3) // n)
        cells.setdefault(f"{factor_x}_t{tx}__{factor_y}_t{ty}", []).append(usable[i][2])

    out: dict = {}
    for key, vals in cells.items():
        if len(vals) < MIN_INTERACTION_CELL:
            out[key] = {"n": len(vals), "insufficient_sample": True}
            continue
        out[key] = {
            "n": len(vals),
            "avg_return": sum(v["return"] for v in vals) / len(vals),
            "hit_rate": sum(1 for v in vals if v["return"] > 0) / len(vals),
        }
    return out


# ---- Phase 2: reproduce Rev.86 (same function calls, same cached data) ------------------------------


def phase2_reproduce(all_samples: dict[str, tuple[list[Sample], list[dict]]], flat: dict[str, list[tuple[Sample, dict]]]) -> dict:
    result: dict = {"hypothetical_long_outcome": {}, "predictive_discrimination": {}, "diff_vs_sparsity_diagnostic": {}}
    for asset_class in ("equity", "crypto"):
        by_bucket: dict[str, list[tuple[Sample, Decimal | None]]] = {name: [] for name, _lo, _hi in COMPOSITE_BUCKETS}
        for symbol, (samples, bars) in all_samples.items():
            if samples and samples[0].asset_class != asset_class:
                continue
            for s in samples:
                bucket = _bucket_for(s.composite, COMPOSITE_BUCKETS)
                if bucket is None:
                    continue
                by_bucket[bucket].append((s, _hypothetical_long_outcome(s, bars)))
        result["hypothetical_long_outcome"][asset_class] = {b: _r_metrics(v) for b, v in by_bucket.items()}
        result["predictive_discrimination"][asset_class] = _composite_bucket_summary(flat[asset_class])

    if SPARSITY_DIAGNOSTIC_PATH.exists():
        prior = json.loads(SPARSITY_DIAGNOSTIC_PATH.read_text(encoding="utf-8"))
        for asset_class in ("equity", "crypto"):
            diffs: dict = {}
            prior_ho = prior.get("bucketed", {}).get("by_composite", {}).get(asset_class, {}).get("hypothetical_long_outcome", {})
            new_ho = result["hypothetical_long_outcome"][asset_class]
            for bucket, prior_metrics in prior_ho.items():
                new_metrics = new_ho.get(bucket, {})
                prior_exp = prior_metrics.get("expectancy_r")
                new_exp = new_metrics.get("expectancy_r")
                if prior_exp is None or new_exp is None:
                    diffs[bucket] = {"prior_expectancy_r": prior_exp, "new_expectancy_r": new_exp, "match": prior_exp == new_exp}
                else:
                    diffs[bucket] = {
                        "prior_expectancy_r": prior_exp, "new_expectancy_r": new_exp,
                        "abs_diff": abs(prior_exp - new_exp), "match": abs(prior_exp - new_exp) < 1e-9,
                    }
            result["diff_vs_sparsity_diagnostic"][asset_class] = diffs
    else:
        result["diff_vs_sparsity_diagnostic"]["error"] = f"{SPARSITY_DIAGNOSTIC_PATH} not found -- cannot diff"

    return result


def main() -> None:
    all_samples: dict[str, tuple[list[Sample], list[dict]]] = {}
    benchmark_bars_by_class: dict[str, list[dict]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"), ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_bars = json.loads((CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json").read_text())["bars"]
        benchmark_bars_by_class[asset_class] = bench_bars
        for symbol in universe:
            samples, bars = generate_daily_samples(symbol, asset_class, bench_bars)
            all_samples[symbol] = (samples, bars)
            print(f"  {symbol}: {len(samples)} daily samples")

    print("Precomputing exit-policy-independent forward metrics (Phase 3/4 base data)...")
    forward_by_symbol: dict[str, list[tuple[Sample, dict]]] = {}
    flat: dict[str, list[tuple[Sample, dict]]] = {"equity": [], "crypto": []}
    for symbol, (samples, bars) in all_samples.items():
        records = [(s, _forward_metrics(s, bars)) for s in samples]
        forward_by_symbol[symbol] = records
        if records:
            flat[records[0][0].asset_class].extend(records)

    print("Phase 2 -- reproducing Rev.86 composite-bucket findings...")
    phase2 = phase2_reproduce(all_samples, flat)
    for asset_class, diffs in phase2["diff_vs_sparsity_diagnostic"].items():
        if asset_class == "error":
            print(f"  WARNING: {diffs}")
            continue
        mismatches = [b for b, d in diffs.items() if not d.get("match", False)]
        print(f"  {asset_class}: {'all buckets match' if not mismatches else f'MISMATCH in {mismatches}'}")

    print("Phase 3 -- monotonicity / information-content analysis...")
    phase3: dict = {"correlation": {}, "deciles": {}, "fold_stability": {}, "symbol_stability": {}, "regime_stability": {}}
    for asset_class in ("equity", "crypto"):
        phase3["correlation"][asset_class] = {f: correlation_table(flat[asset_class], f) for f in FACTOR_NAMES}
        phase3["deciles"][asset_class] = {f: decile_table(flat[asset_class], f) for f in FACTOR_NAMES}
    phase3["fold_stability"] = fold_stability(flat)
    phase3["symbol_stability"] = symbol_stability(all_samples, forward_by_symbol)
    tagged = tag_regimes(flat, benchmark_bars_by_class)
    phase3["regime_stability"] = regime_stability(tagged)

    print("Phase 4 -- interaction analysis...")
    phase4: dict = {}
    for asset_class in ("equity", "crypto"):
        phase4[asset_class] = {
            "momentum_x_technical": interaction_table(flat[asset_class], "momentum_score", "technical_score"),
            "technical_x_risk": interaction_table(flat[asset_class], "technical_score", "risk_score"),
            "momentum_x_risk": interaction_table(flat[asset_class], "momentum_score", "risk_score"),
        }

    result = {"phase2_reproduction": phase2, "phase3_monotonicity": phase3, "phase4_interactions": phase4}
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Wrote entry-composite audit results to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
