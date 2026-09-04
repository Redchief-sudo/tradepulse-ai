"""Read-only diagnostic: is crypto's fixed-composite BUY-signal sparsity
(see docs/exit-parameter-calibration.md's "crypto fixed-composite signal
sparsity" finding) explained by (A) the sparsity being justified, (B) the
universal 65 threshold being mis-set for crypto, (C) one of the three live
factors (technical/momentum/risk -- the only ones with nonzero weight in
today's fixed baseline) being equity-shaped, some combination, or (D)
insufficient evidence?

Produces data/calibration/sparsity_diagnostic.json only. No tradepulse/
file is touched, no weight/threshold/formula is changed. See the plan's
explicit A/B/C/B+C/D framework -- this script computes the numbers; it does
not decide the classification (that's judgment applied to the numbers in
the report).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOL_DIR))

from tradepulse.config import default_strategy_weights, risk_limits_for_profile  # noqa: E402
from tradepulse.models import AssetClass  # noqa: E402
from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price  # noqa: E402
from tradepulse.strategy import compute_real_factors, signal_from_composite, weighted_composite  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
from simulate_trades import (  # noqa: E402
    CACHE_ROOT, MIN_CANDLES, Entry, _benchmark_closes_as_of, _to_candle, simulate_exit,
)

RESULTS_PATH = CACHE_ROOT / "sparsity_diagnostic.json"

COMPOSITE_BUCKETS = [
    ("<45", None, Decimal("45")), ("45-49", Decimal("45"), Decimal("50")), ("50-54", Decimal("50"), Decimal("55")),
    ("55-59", Decimal("55"), Decimal("60")), ("60-64", Decimal("60"), Decimal("65")),
    ("65-69", Decimal("65"), Decimal("70")), ("70+", Decimal("70"), None),
]
HORIZONS = [1, 3, 5, 10, 15]

# Fixed, decoupled from the exit-parameter grid sweep -- balanced profile's
# already-in-production calibrated values, used only so 4a has SOME exit
# policy to evaluate against, never re-optimized here.
_BALANCED = risk_limits_for_profile("balanced")
BREAK_EVEN_TRIGGER_PCT = _BALANCED.break_even_trigger_pct
MAX_HOLD_DAYS = _BALANCED.max_hold_days
TRAILING_ATR_MULTIPLIER = _BALANCED.trailing_atr_multiplier

ENTRY_ATR_MULTIPLIER = Decimal("2")
ENTRY_MIN_STOP_DISTANCE_PCT = Decimal("0.5")
ENTRY_MAX_STOP_DISTANCE_PCT = Decimal("25")
ENTRY_FALLBACK_STOP_LOSS_PCT = Decimal("8")


@dataclass(frozen=True, slots=True)
class Sample:
    symbol: str
    asset_class: str
    date: str
    index: int
    technical_score: Decimal
    momentum_score: Decimal
    risk_score: Decimal
    liquidity_score: Decimal
    risk_quality_score: Decimal
    relative_strength_score: Decimal | None
    composite: Decimal
    signal: str


def generate_daily_samples(symbol: str, asset_class: str, benchmark_bars: list[dict]) -> tuple[list[Sample], list[dict]]:
    """Every day, unfiltered (unlike simulate_trades.generate_entries, which
    only keeps BUY/STRONG_BUY days) -- this is what lets §2/§3/§4b/§4c see
    the FULL distribution, not just the sparse signal tail."""
    data = json.loads((CACHE_ROOT / asset_class / f"{symbol.replace('/', '-')}.json").read_text(encoding="utf-8"))
    bars = data["bars"]
    calendar = "crypto" if asset_class == "crypto" else "equity"
    weights = default_strategy_weights(__import__("datetime").datetime.now(__import__("datetime").UTC))

    samples: list[Sample] = []
    for i in range(MIN_CANDLES - 1, len(bars)):
        window = bars[max(0, i - 249) : i + 1]
        candles = [_to_candle(b) for b in window]
        as_of_date = bars[i]["date"]
        bench_closes = _benchmark_closes_as_of(benchmark_bars, as_of_date)
        scores = compute_real_factors(candles, calendar=calendar, benchmark_closes=bench_closes if bench_closes else None)
        if scores is None:
            continue
        composite = weighted_composite(scores, weights)
        signal = signal_from_composite(composite)
        samples.append(Sample(
            symbol=symbol, asset_class=asset_class, date=as_of_date, index=i,
            technical_score=scores.technical_score, momentum_score=scores.momentum_score, risk_score=scores.risk_score,
            liquidity_score=scores.liquidity_score, risk_quality_score=scores.risk_quality_score,
            relative_strength_score=scores.relative_strength_score, composite=composite, signal=signal,
        ))
    return samples, bars


def _percentiles(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(n - 1, int(n * p))
        return s[idx]

    return {"p10": pct(0.10), "p25": pct(0.25), "median": pct(0.50), "p75": pct(0.75), "p90": pct(0.90), "n": n}


def _bucket_for(value: Decimal, buckets: list[tuple[str, Decimal | None, Decimal | None]]) -> str | None:
    for name, lo, hi in buckets:
        if lo is not None and value < lo:
            continue
        if hi is not None and value >= hi:
            continue
        return name
    return None


def _hypothetical_long_outcome(sample: Sample, bars: list[dict]) -> Decimal | None:
    """4a -- exit-policy-dependent. Named "hypothetical long outcome",
    never "production expectancy": production never entered on sub-
    threshold observations."""
    entry_price = Decimal(bars[sample.index]["close"])
    asset_cls_enum = AssetClass.CRYPTO if sample.asset_class == "crypto" else AssetClass.EQUITY
    candles = [_to_candle(b) for b in bars[max(0, sample.index - 249) : sample.index + 1]]
    atr_stop = _atr_stop_loss_price(entry_price, candles, ENTRY_ATR_MULTIPLIER, asset_cls_enum, ENTRY_MIN_STOP_DISTANCE_PCT, ENTRY_MAX_STOP_DISTANCE_PCT)
    initial_stop = atr_stop if atr_stop is not None else _stop_loss_price(entry_price, ENTRY_FALLBACK_STOP_LOSS_PCT, asset_cls_enum)
    entry = Entry(sample.symbol, sample.asset_class, sample.date, sample.index, entry_price, initial_stop, sample.signal)
    outcome = simulate_exit(entry, bars, BREAK_EVEN_TRIGGER_PCT, MAX_HOLD_DAYS, TRAILING_ATR_MULTIPLIER)
    return outcome.r_multiple


def _forward_metrics(sample: Sample, bars: list[dict]) -> dict[int, dict]:
    """4b/4c -- exit-policy-INDEPENDENT. Pure forward return/MFE/MAE from
    closes/highs/lows only, no stop, no time-stop, no simulation."""
    entry_close = Decimal(bars[sample.index]["close"])
    result: dict[int, dict] = {}
    for horizon in HORIZONS:
        end = sample.index + horizon
        if end >= len(bars):
            continue  # censored for this horizon -- excluded, never padded/truncated
        window = bars[sample.index + 1 : end + 1]
        fwd_return = Decimal(window[-1]["close"]) / entry_close - 1
        mfe = max(Decimal(b["high"]) for b in window) / entry_close - 1
        mae = min(Decimal(b["low"]) for b in window) / entry_close - 1
        result[horizon] = {"return": float(fwd_return), "mfe": float(mfe), "mae": float(mae)}
    return result


def _aggregate_bucket_metrics(entries: list[tuple[Sample, dict]]) -> dict:
    """entries: list of (sample, {horizon: {return, mfe, mae}}). Averages
    each horizon's metrics across whatever samples have that horizon
    (censored ones simply don't contribute to that horizon)."""
    out: dict = {
        "sample_count": len(entries),
        "unique_symbols": len({s.symbol for s, _ in entries}),
        "unique_dates": len({s.date for s, _ in entries}),
    }
    for horizon in HORIZONS:
        vals = [m[horizon] for _s, m in entries if horizon in m]
        if not vals:
            continue
        out[f"h{horizon}"] = {
            "n": len(vals),
            "avg_return": sum(v["return"] for v in vals) / len(vals),
            "avg_mfe": sum(v["mfe"] for v in vals) / len(vals),
            "avg_mae": sum(v["mae"] for v in vals) / len(vals),
        }
    return out


def _r_metrics(entries: list[tuple[Sample, Decimal | None]]) -> dict:
    rs = [r for _s, r in entries if r is not None]
    censored = sum(1 for _s, r in entries if r is None)
    out = {
        "sample_count": len(entries), "censored": censored,
        "unique_symbols": len({s.symbol for s, _r in entries}),
        "unique_dates": len({s.date for s, _r in entries}),
    }
    if rs:
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        gross_win = sum(wins, Decimal("0"))
        gross_loss = abs(sum(losses, Decimal("0")))
        out["hit_rate"] = len(wins) / len(rs)
        out["expectancy_r"] = float(sum(rs, Decimal("0")) / len(rs))
        out["profit_factor"] = float(gross_win / gross_loss) if gross_loss > 0 else None
    return out


def main() -> None:
    all_samples: dict[str, tuple[list[Sample], list[dict]]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"), ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_bars = json.loads((CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json").read_text())["bars"]
        for symbol in universe:
            samples, bars = generate_daily_samples(symbol, asset_class, bench_bars)
            all_samples[symbol] = (samples, bars)
            print(f"  {symbol}: {len(samples)} daily samples")

    result: dict = {"symbol_summary": {}, "distributions": {}, "threshold_sensitivity": {}, "bucketed": {}}

    # Per-symbol summary (§2's evidence table, e.g. BCH/USD reproduction).
    for symbol, (samples, _bars) in all_samples.items():
        composites = [float(s.composite) for s in samples]
        gaps = json.loads((CACHE_ROOT / samples[0].asset_class / f"{symbol.replace('/', '-')}.json").read_text())["gaps"] if samples else []
        result["symbol_summary"][symbol] = {
            "asset_class": samples[0].asset_class if samples else None,
            "bar_count": len(samples), "gap_count": len(gaps),
            **_percentiles(composites),
            "count_above_65": sum(1 for c in composites if c > 65),
            "pct_above_65": (sum(1 for c in composites if c > 65) / len(composites)) if composites else None,
        }

    # §2: equity vs crypto distributions, all 6 factors + composite.
    factor_names = ["technical_score", "momentum_score", "risk_score", "liquidity_score", "risk_quality_score", "relative_strength_score", "composite"]
    for asset_class in ("equity", "crypto"):
        flat = [s for samples, _bars in all_samples.values() for s in samples if s.asset_class == asset_class]
        result["distributions"][asset_class] = {}
        for factor in factor_names:
            vals = [float(getattr(s, factor)) for s in flat if getattr(s, factor) is not None]
            result["distributions"][asset_class][factor] = _percentiles(vals)
        signal_counts: dict[str, int] = {}
        for s in flat:
            signal_counts[s.signal] = signal_counts.get(s.signal, 0) + 1
        result["distributions"][asset_class]["signal_counts"] = signal_counts
        result["distributions"][asset_class]["total"] = len(flat)

    # §3: threshold sensitivity, diagnostic only.
    for asset_class in ("equity", "crypto"):
        flat = [s for samples, _bars in all_samples.values() for s in samples if s.asset_class == asset_class]
        n = len(flat)
        result["threshold_sensitivity"][asset_class] = {
            str(t): (sum(1 for s in flat if s.composite > t) / n if n else None) for t in (55, 60, 65, 70)
        }

    # §4a/4b: bucketed by composite.
    print("Computing §4a (hypothetical long outcome by composite bucket)...")
    for asset_class in ("equity", "crypto"):
        by_bucket: dict[str, list[tuple[Sample, Decimal | None]]] = {name: [] for name, _lo, _hi in COMPOSITE_BUCKETS}
        by_bucket_fwd: dict[str, list[tuple[Sample, dict]]] = {name: [] for name, _lo, _hi in COMPOSITE_BUCKETS}
        for symbol, (samples, bars) in all_samples.items():
            if samples and samples[0].asset_class != asset_class:
                continue
            for s in samples:
                bucket = _bucket_for(s.composite, COMPOSITE_BUCKETS)
                if bucket is None:
                    continue
                by_bucket[bucket].append((s, _hypothetical_long_outcome(s, bars)))
                by_bucket_fwd[bucket].append((s, _forward_metrics(s, bars)))
        result["bucketed"].setdefault("by_composite", {})[asset_class] = {
            "hypothetical_long_outcome": {b: _r_metrics(v) for b, v in by_bucket.items()},
            "predictive_discrimination": {b: _aggregate_bucket_metrics(v) for b, v in by_bucket_fwd.items()},
        }

    # §4c: bucketed by each of the three live factors independently.
    print("Computing §4c (per-factor predictive discrimination)...")
    factor_buckets = [
        ("<20", None, Decimal("20")), ("20-39", Decimal("20"), Decimal("40")), ("40-59", Decimal("40"), Decimal("60")),
        ("60-79", Decimal("60"), Decimal("80")), ("80+", Decimal("80"), None),
    ]
    for live_factor in ("technical_score", "momentum_score", "risk_score"):
        result["bucketed"].setdefault("by_factor", {})[live_factor] = {}
        for asset_class in ("equity", "crypto"):
            by_bucket_fwd = {name: [] for name, _lo, _hi in factor_buckets}
            for symbol, (samples, bars) in all_samples.items():
                if samples and samples[0].asset_class != asset_class:
                    continue
                for s in samples:
                    bucket = _bucket_for(getattr(s, live_factor), factor_buckets)
                    if bucket is None:
                        continue
                    by_bucket_fwd[bucket].append((s, _forward_metrics(s, bars)))
            result["bucketed"]["by_factor"][live_factor][asset_class] = {b: _aggregate_bucket_metrics(v) for b, v in by_bucket_fwd.items()}

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote diagnostic results to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
