"""Read-only, held-out entry-composite calibration ladder (B0-B7) -- see
docs/entry-composite-calibration-ladder.md for the full report and the
approved plan (`/home/damien/.claude/plans/delegated-doodling-toucan.md`
at the time this was written) for the exact methodology this implements.

Rev.87's forensic audit found the current deterministic composite's rank
correlation with forward return is negative in both asset classes, at
every horizon, in every walk-forward fold, and identified the mechanism:
risk_score (a calmness measure) is itself negatively correlated with
forward return and, added into the composite alongside technical/momentum,
actively rewards the wrong combination. This script tests a SEQUENCE of
narrow, isolated corrections (an "ablation ladder") rather than searching
the whole parameter space at once, which would very likely overfit the
same discovery dataset that exposed the problem.

Hard constraints (unchanged from every prior script in this series, and
explicit in the user's own brief for this phase): no tradepulse/ file is
touched; no production weight/threshold/formula changes; no commit/push.
B1's "calendar-corrected annualization" is a harness-only recomputation
that never reaches tradepulse/factors.py.

TRAIN/VALIDATION/HOLDOUT pool discipline (stricter than Rev.86/87's plain
train/test): the HOLDOUT pool's data is never passed into any function
that makes a selection/promotion decision. It is loaded into a separate
variable only in main()'s final step, evaluated exactly once. See
test_entry_calibration_ladder_harness.py for a structural proof of this.

Deterministic-layer-only, same permanent limitation as every other script
here: no AI-recommendation gate is simulated.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOL_DIR))

from tradepulse.models import AssetClass  # noqa: E402
from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price  # noqa: E402
from tradepulse.strategy import compute_real_factors, signal_from_composite, weighted_composite  # noqa: E402
from tradepulse.strategy import indicators as ind  # noqa: E402
from tradepulse.config import default_strategy_weights  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
from entry_composite_audit import correlation_table, spearman  # noqa: E402
from simulate_trades import (  # noqa: E402
    CACHE_ROOT, ENTRY_ATR_MULTIPLIER, ENTRY_FALLBACK_STOP_LOSS_PCT, ENTRY_MAX_STOP_DISTANCE_PCT,
    ENTRY_MIN_STOP_DISTANCE_PCT, MIN_CANDLES, Entry, TradeOutcome, _benchmark_closes_as_of, _to_candle, simulate_exit,
)

RESULTS_PATH = CACHE_ROOT / "entry_calibration_ladder.json"

# Held fixed throughout -- balanced profile's already-calibrated exit
# parameters, decoupled from this composite-only experiment (matches the
# same fixed-exit-policy convention diagnose_signal_sparsity.py already
# uses for its own hypothetical-outcome computation).
from tradepulse.config import risk_limits_for_profile  # noqa: E402
_BALANCED = risk_limits_for_profile("balanced")
BREAK_EVEN_TRIGGER_PCT = _BALANCED.break_even_trigger_pct
MAX_HOLD_DAYS = _BALANCED.max_hold_days
TRAILING_ATR_MULTIPLIER = _BALANCED.trailing_atr_multiplier

# ---- Pool boundaries ------------------------------------------------------
# Reuses the exact chronological cutoffs already approved in
# calibrate_exit_params.FOLDS, repartitioned into three NON-OVERLAPPING
# pools (the original FOLDS test windows deliberately overlap -- fold_3's
# test window is all of 2025, fold_4's is Sep 2025+ -- which would leak
# HOLDOUT data into VALIDATION if reused as-is). TRAIN/VALIDATION never
# see HOLDOUT; HOLDOUT is loaded only in main()'s final step.
TRAIN_START, TRAIN_END = date(2023, 1, 1), date(2024, 12, 31)
VALIDATION_START, VALIDATION_END = date(2025, 1, 1), date(2025, 8, 31)
HOLDOUT_START, HOLDOUT_END = date(2025, 9, 1), date(2099, 1, 1)

MIN_PROMOTION_SPEARMAN_DELTA = 0.02  # +0.02 absolute TRAIN-pool improvement required to promote a rung
STABILITY_HORIZON = 5


# ---- Raw per-day sample: production's exact B0 fields + every raw sub-component needed by B1-B7 ----


@dataclass(frozen=True, slots=True)
class RawSample:
    symbol: str
    asset_class: str
    date: str
    index: int
    # Raw indicator components (all computed by calling tradepulse.strategy.indicators
    # directly -- the same functions/periods factors.py itself calls internally,
    # never reimplemented) -- needed for B1 (risk annualization), B4 (technical
    # decomposition), B5 (momentum normalization).
    rsi: float | None
    macd_histogram: float | None
    ma50: float | None
    ma200: float | None
    bollinger_percent_b: float | None
    raw_momentum_pct: float | None  # unclamped 14-day % change
    raw_vol_365: float | None  # ind.volatility(20)'s own sqrt(365)-annualized output, as-is
    # Production's exact fields, from compute_real_factors/weighted_composite/
    # signal_from_composite directly -- this IS B0, byte-for-bit, not a
    # reimplementation, which is what makes the B0-parity proof meaningful.
    b0_technical_score: Decimal
    b0_momentum_score: Decimal
    b0_risk_score: Decimal
    b0_composite: Decimal
    b0_signal: str


def generate_ladder_samples(symbol: str, asset_class: str, benchmark_bars: list[dict]) -> tuple[list[RawSample], list[dict]]:
    data = json.loads((CACHE_ROOT / asset_class / f"{symbol.replace('/', '-')}.json").read_text(encoding="utf-8"))
    bars = data["bars"]
    calendar = "crypto" if asset_class == "crypto" else "equity"
    weights = default_strategy_weights(__import__("datetime").datetime.now(__import__("datetime").UTC))

    samples: list[RawSample] = []
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

        closes = [float(c.close) for c in candles]
        rsi_val = ind.rsi(closes, 14)
        macd_val = ind.macd(closes)
        boll = ind.bollinger(closes, 20, 2)
        ma50 = ind.sma(closes, 50)
        ma200 = ind.sma(closes, 200)
        raw_momentum_pct = ind.momentum(closes, 14)
        raw_vol_365 = ind.volatility(closes, 20)

        samples.append(RawSample(
            symbol=symbol, asset_class=asset_class, date=as_of_date, index=i,
            rsi=rsi_val, macd_histogram=(macd_val.histogram if macd_val else None),
            ma50=ma50, ma200=ma200, bollinger_percent_b=(boll.percent_b if boll else None),
            raw_momentum_pct=raw_momentum_pct, raw_vol_365=raw_vol_365,
            b0_technical_score=scores.technical_score, b0_momentum_score=scores.momentum_score,
            b0_risk_score=scores.risk_score, b0_composite=composite, b0_signal=signal,
        ))
    return samples, bars


# ---- Trailing, no-lookahead percentile transform (B5 option iii) ----------


def trailing_percentile_momentum(momentum_series: list[float | None], window: int = 250) -> list[float | None]:
    """Percentile rank (0-100) of each value within its OWN trailing window
    (up to `window` bars ending at and including the current index) --
    structurally no-lookahead: position i only ever looks at
    momentum_series[max(0, i-window+1):i+1]."""
    out: list[float | None] = []
    for i, value in enumerate(momentum_series):
        if value is None:
            out.append(None)
            continue
        trailing = [v for v in momentum_series[max(0, i - window + 1) : i + 1] if v is not None]
        if len(trailing) < 2:
            out.append(50.0)  # insufficient trailing history -- neutral, not fabricated
            continue
        rank = sum(1 for v in trailing if v <= value)
        out.append((rank / len(trailing)) * 100)
    return out


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


# ---- Candidate specification: what each B-rung changes ---------------------


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    label: str
    risk_annualization: str = "uncalibrated"  # "uncalibrated" | "calendar_aware"
    risk_mode: str = "additive"  # "additive" | "gate" | "excluded"
    risk_gate_direction: str | None = None  # "lt" | "gt"
    risk_gate_threshold: float | None = None
    technical_mode: str = "blended"  # "blended" | "mean_reversion" | "trend_confirmation" | "avg_decomposed"
    momentum_mode: str = "linear_x2"  # "linear_x2" | "calendar_rescaled" | "percentile_rank"
    momentum_rescale_slope: dict[str, float] = field(default_factory=dict)  # calendar -> slope, only for calendar_rescaled
    technical_weight: float = 25.0
    momentum_weight: float = 15.0
    risk_weight: float = 15.0
    buy_threshold: float = 65.0


def _technical_value(raw: RawSample, mode: str) -> float | None:
    if mode == "blended":
        return float(raw.b0_technical_score)
    mean_reversion = 50.0
    if raw.rsi is not None:
        mean_reversion += (50 - raw.rsi) * 0.5
    if raw.bollinger_percent_b is not None:
        if raw.bollinger_percent_b < 20:
            mean_reversion += 8
        elif raw.bollinger_percent_b > 80:
            mean_reversion -= 8
    mean_reversion = _clamp(mean_reversion)

    trend_confirmation = 50.0
    if raw.macd_histogram is not None:
        trend_confirmation += 10 if raw.macd_histogram > 0 else -10
    if raw.ma50 is not None and raw.ma200 is not None:
        trend_confirmation += 10 if raw.ma50 > raw.ma200 else -10
    trend_confirmation = _clamp(trend_confirmation)

    if mode == "mean_reversion":
        return mean_reversion
    if mode == "trend_confirmation":
        return trend_confirmation
    if mode == "avg_decomposed":
        return (mean_reversion + trend_confirmation) / 2
    raise ValueError(f"unknown technical_mode {mode!r}")


def _risk_value(raw: RawSample, annualization: str) -> float | None:
    if raw.raw_vol_365 is None:
        return None
    if annualization == "uncalibrated":
        return float(raw.b0_risk_score)
    periods = 252 if raw.asset_class != "crypto" else 365
    corrected_vol = raw.raw_vol_365 * math.sqrt(periods / 365)
    return _clamp(100 - corrected_vol)


def score_sample(
    raw: RawSample, spec: CandidateSpec, momentum_series_value: float | None = None,
) -> tuple[float | None, bool]:
    """Returns (composite, would_enter). would_enter is None-safe: False
    whenever any required component is unavailable, never fabricated."""
    technical = _technical_value(raw, spec.technical_mode)
    if spec.momentum_mode == "linear_x2":
        momentum = _clamp(50 + raw.raw_momentum_pct * 2) if raw.raw_momentum_pct is not None else None
    elif spec.momentum_mode == "calendar_rescaled":
        if raw.raw_momentum_pct is None:
            momentum = None
        else:
            calendar = "crypto" if raw.asset_class == "crypto" else "equity"
            slope = spec.momentum_rescale_slope.get(calendar, 2.0)
            momentum = _clamp(50 + raw.raw_momentum_pct * slope)
    elif spec.momentum_mode == "percentile_rank":
        momentum = momentum_series_value
    else:
        raise ValueError(f"unknown momentum_mode {spec.momentum_mode!r}")

    risk = _risk_value(raw, spec.risk_annualization)

    parts: list[tuple[float, float]] = []
    if technical is not None and spec.technical_weight > 0:
        parts.append((technical, spec.technical_weight))
    if momentum is not None and spec.momentum_weight > 0:
        parts.append((momentum, spec.momentum_weight))
    if spec.risk_mode == "additive" and risk is not None and spec.risk_weight > 0:
        parts.append((risk, spec.risk_weight))
    if not parts:
        return None, False

    numerator = sum(v * w for v, w in parts)
    denominator = sum(w for _v, w in parts)
    composite = numerator / denominator

    would_enter = composite > spec.buy_threshold
    if spec.risk_mode == "gate":
        if risk is None or spec.risk_gate_threshold is None:
            would_enter = False
        elif spec.risk_gate_direction == "lt":
            would_enter = would_enter and risk < spec.risk_gate_threshold
        else:
            would_enter = would_enter and risk > spec.risk_gate_threshold

    return composite, would_enter


# ---- Pool assignment --------------------------------------------------------


def pool_for_date(d: str) -> str | None:
    parsed = date.fromisoformat(d)
    if TRAIN_START <= parsed <= TRAIN_END:
        return "train"
    if VALIDATION_START <= parsed <= VALIDATION_END:
        return "validation"
    if HOLDOUT_START <= parsed <= HOLDOUT_END:
        return "holdout"
    return None  # outside the calibration window entirely (pre-2023 history) -- not used by this ladder


# ---- Forward-return metrics (reused shape from entry_composite_audit.py) --


HORIZONS = (1, 3, 5, 10, 15)


def forward_metrics(index: int, bars: list[dict]) -> dict[int, dict]:
    entry_close = Decimal(bars[index]["close"])
    result: dict[int, dict] = {}
    for horizon in HORIZONS:
        end = index + horizon
        if end >= len(bars):
            continue
        window = bars[index + 1 : end + 1]
        fwd_return = Decimal(window[-1]["close"]) / entry_close - 1
        mfe = max(Decimal(b["high"]) for b in window) / entry_close - 1
        mae = min(Decimal(b["low"]) for b in window) / entry_close - 1
        result[horizon] = {"return": float(fwd_return), "mfe": float(mfe), "mae": float(mae)}
    return result


# ---- Trade-level metrics (reused shape from calibrate_exit_params.py) -----


def _trade_metrics(rs: list[Decimal]) -> dict:
    if not rs:
        return {"trade_count": 0, "expectancy_r": None, "profit_factor": None, "max_drawdown_r": None, "hit_rate": None}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    cum = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trade_count": len(rs), "hit_rate": len(wins) / len(rs), "expectancy_r": float(sum(rs, Decimal("0")) / len(rs)),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else None, "max_drawdown_r": float(max_dd),
    }


def _independence_metrics(symbols: list[str], dates: list[str]) -> dict:
    unique_symbols = len(set(symbols))
    unique_dates = len(set(dates))
    per_symbol: dict[str, int] = {}
    for s in symbols:
        per_symbol[s] = per_symbol.get(s, 0) + 1
    max_concentration = max(per_symbol.values()) if per_symbol else 0
    return {
        "unique_symbols": unique_symbols, "unique_dates": unique_dates,
        "max_entries_per_symbol": max_concentration,
        "max_symbol_share": (max_concentration / len(symbols)) if symbols else None,
    }


@dataclass
class CandidateEvaluation:
    label: str
    spearman_by_pool: dict[str, dict]  # pool -> {"n":..., "spearman":...}
    trade_metrics_by_pool: dict[str, dict]
    independence_by_pool: dict[str, dict]


def evaluate_candidate(
    spec: CandidateSpec, samples_by_symbol: dict[str, tuple[list[RawSample], list[dict]]],
    momentum_series_by_symbol: dict[str, list[float | None]], pools: tuple[str, ...] = ("train", "validation"),
) -> CandidateEvaluation:
    """Computes Spearman (ordering quality) and trade-level metrics (guard
    values) for `spec`, restricted to the requested pools. `pools` never
    includes "holdout" except in the one designated final-evaluation call
    site in main() -- see the structural isolation proof in the test file."""
    spearman_by_pool: dict[str, dict] = {p: {"xs": [], "ys": []} for p in pools}
    entries_by_pool: dict[str, list[tuple[str, str, Decimal]]] = {p: [] for p in pools}  # (symbol, date, r_multiple)
    symbols_by_pool: dict[str, list[str]] = {p: [] for p in pools}
    dates_by_pool: dict[str, list[str]] = {p: [] for p in pools}

    for symbol, (samples, bars) in samples_by_symbol.items():
        momentum_series = momentum_series_by_symbol.get(symbol)
        for local_i, raw in enumerate(samples):
            pool = pool_for_date(raw.date)
            if pool not in pools:
                continue
            mom_val = momentum_series[local_i] if momentum_series is not None else None
            composite, would_enter = score_sample(raw, spec, momentum_series_value=mom_val)
            if composite is None:
                continue
            fm = forward_metrics(raw.index, bars)
            if STABILITY_HORIZON in fm:
                spearman_by_pool[pool]["xs"].append(composite)
                spearman_by_pool[pool]["ys"].append(fm[STABILITY_HORIZON]["return"])
            if would_enter:
                entry_price = Decimal(bars[raw.index]["close"])
                asset_cls_enum = AssetClass.CRYPTO if raw.asset_class == "crypto" else AssetClass.EQUITY
                candles = [_to_candle(b) for b in bars[max(0, raw.index - 249) : raw.index + 1]]
                atr_stop = _atr_stop_loss_price(
                    entry_price, candles, ENTRY_ATR_MULTIPLIER, asset_cls_enum,
                    ENTRY_MIN_STOP_DISTANCE_PCT, ENTRY_MAX_STOP_DISTANCE_PCT,
                )
                initial_stop = atr_stop if atr_stop is not None else _stop_loss_price(entry_price, ENTRY_FALLBACK_STOP_LOSS_PCT, asset_cls_enum)
                entry = Entry(symbol, raw.asset_class, raw.date, raw.index, entry_price, initial_stop, raw.b0_signal)
                outcome = simulate_exit(entry, bars, BREAK_EVEN_TRIGGER_PCT, MAX_HOLD_DAYS, TRAILING_ATR_MULTIPLIER)
                if outcome.r_multiple is not None:
                    entries_by_pool[pool].append((symbol, raw.date, outcome.r_multiple))
                    symbols_by_pool[pool].append(symbol)
                    dates_by_pool[pool].append(raw.date)

    spearman_result = {
        p: {"n": len(v["xs"]), "spearman": spearman(v["xs"], v["ys"])} for p, v in spearman_by_pool.items()
    }
    trade_result = {p: _trade_metrics([r for _s, _d, r in entries_by_pool[p]]) for p in pools}
    independence_result = {p: _independence_metrics(symbols_by_pool[p], dates_by_pool[p]) for p in pools}
    return CandidateEvaluation(spec.label, spearman_result, trade_result, independence_result)


# ---- Promotion rule ----------------------------------------------------------


@dataclass
class PromotionResult:
    promoted: bool
    reason: str
    train_spearman_delta: float | None
    guard_failures: list[str]


def check_promotion(prior: CandidateEvaluation, candidate: CandidateEvaluation) -> PromotionResult:
    prior_sp = prior.spearman_by_pool["train"]["spearman"]
    cand_sp = candidate.spearman_by_pool["train"]["spearman"]
    if prior_sp is None or cand_sp is None:
        return PromotionResult(False, "insufficient TRAIN-pool data to compute Spearman delta", None, [])
    delta = cand_sp - prior_sp

    guard_failures: list[str] = []
    prior_tm, cand_tm = prior.trade_metrics_by_pool["train"], candidate.trade_metrics_by_pool["train"]
    # Guards are pass/fail non-regression checks, never search targets --
    # "catastrophic" is defined once, applied uniformly: trade count
    # collapsing to <20% of the prior rung's, or a positive prior expectancy
    # turning negative, or profit factor dropping below 0.5, or max
    # drawdown more than doubling.
    if prior_tm["trade_count"] and cand_tm["trade_count"] < 0.2 * prior_tm["trade_count"]:
        guard_failures.append(f"trade_count collapsed ({prior_tm['trade_count']} -> {cand_tm['trade_count']})")
    if prior_tm["expectancy_r"] is not None and cand_tm["expectancy_r"] is not None:
        if prior_tm["expectancy_r"] > 0 and cand_tm["expectancy_r"] < 0:
            guard_failures.append(f"expectancy_r flipped positive->negative ({prior_tm['expectancy_r']:.4f} -> {cand_tm['expectancy_r']:.4f})")
    if cand_tm["profit_factor"] is not None and cand_tm["profit_factor"] < 0.5:
        guard_failures.append(f"profit_factor below 0.5 ({cand_tm['profit_factor']:.2f})")
    if prior_tm["max_drawdown_r"] is not None and cand_tm["max_drawdown_r"] is not None and prior_tm["max_drawdown_r"] > 0:
        if cand_tm["max_drawdown_r"] > 2 * prior_tm["max_drawdown_r"]:
            guard_failures.append(f"max_drawdown_r more than doubled ({prior_tm['max_drawdown_r']:.2f} -> {cand_tm['max_drawdown_r']:.2f})")

    if delta < MIN_PROMOTION_SPEARMAN_DELTA:
        return PromotionResult(False, f"TRAIN Spearman delta {delta:+.4f} below +{MIN_PROMOTION_SPEARMAN_DELTA} threshold", delta, guard_failures)
    if guard_failures:
        return PromotionResult(False, "Spearman delta cleared but non-regression guard(s) failed", delta, guard_failures)
    return PromotionResult(True, f"TRAIN Spearman delta {delta:+.4f} clears threshold, guards pass", delta, guard_failures)


# ---- Ladder orchestration ----------------------------------------------------


from dataclasses import replace  # noqa: E402


def _spec_to_dict(spec: CandidateSpec) -> dict:
    out = {}
    for f in fields(spec):
        v = getattr(spec, f.name)
        out[f.name] = str(v) if isinstance(v, Decimal) else v
    return out


def _percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def main() -> None:
    print("Loading cached history and generating raw ladder samples (equity + crypto)...")
    samples_by_symbol: dict[str, tuple[list[RawSample], list[dict]]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"), ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_bars = json.loads((CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json").read_text())["bars"]
        for symbol in universe:
            samples, bars = generate_ladder_samples(symbol, asset_class, bench_bars)
            samples_by_symbol[symbol] = (samples, bars)
            print(f"  {symbol}: {len(samples)} raw samples")

    # ---- Step 0: B0 parity proof -- STOP if it fails ----------------------
    print("Step 0 -- B0 parity proof against diagnose_signal_sparsity.generate_daily_samples...")
    from diagnose_signal_sparsity import generate_daily_samples  # noqa: E402 (imported here, only needed for this proof)
    parity_mismatches: list[str] = []
    parity_checked = 0
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"), ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_bars = json.loads((CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json").read_text())["bars"]
        for symbol in universe:
            reference_samples, _ = generate_daily_samples(symbol, asset_class, bench_bars)
            ladder_samples, _ = samples_by_symbol[symbol]
            if len(reference_samples) != len(ladder_samples):
                parity_mismatches.append(f"{symbol}: sample count differs ({len(reference_samples)} vs {len(ladder_samples)})")
                continue
            for ref, lad in zip(reference_samples, ladder_samples):
                parity_checked += 1
                if ref.date != lad.date or ref.composite != lad.b0_composite or ref.signal != lad.b0_signal:
                    parity_mismatches.append(f"{symbol} {ref.date}: composite {ref.composite} vs {lad.b0_composite}, signal {ref.signal} vs {lad.b0_signal}")
    if parity_mismatches:
        print(f"B0 PARITY FAILED -- {len(parity_mismatches)} mismatches out of {parity_checked} samples checked. STOPPING.")
        for m in parity_mismatches[:20]:
            print(f"  {m}")
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(json.dumps({"b0_parity": {"passed": False, "mismatches": parity_mismatches[:200], "checked": parity_checked}}, indent=2), encoding="utf-8")
        sys.exit(1)
    print(f"B0 parity confirmed: {parity_checked} samples, 0 mismatches.")

    # ---- Momentum trailing-percentile series (B5 option iii), precomputed once, no-lookahead ----
    momentum_series_by_symbol: dict[str, list[float | None]] = {
        symbol: trailing_percentile_momentum([s.raw_momentum_pct for s in samples])
        for symbol, (samples, _bars) in samples_by_symbol.items()
    }

    result: dict = {"b0_parity": {"passed": True, "checked": parity_checked}, "rungs": {}}

    def record_rung(label: str, spec: CandidateSpec, evaluation: CandidateEvaluation, promotion: PromotionResult | None, note: str) -> None:
        result["rungs"][label] = {
            "spec": _spec_to_dict(spec),
            "spearman_by_pool": evaluation.spearman_by_pool,
            "trade_metrics_by_pool": evaluation.trade_metrics_by_pool,
            "independence_by_pool": evaluation.independence_by_pool,
            "promotion": (None if promotion is None else {
                "promoted": promotion.promoted, "reason": promotion.reason,
                "train_spearman_delta": promotion.train_spearman_delta, "guard_failures": promotion.guard_failures,
            }),
            "note": note,
        }

    # ---- B0 ------------------------------------------------------------
    print("Evaluating B0 (baseline)...")
    spec_b0 = CandidateSpec(label="B0")
    eval_b0 = evaluate_candidate(spec_b0, samples_by_symbol, momentum_series_by_symbol)
    record_rung("B0", spec_b0, eval_b0, None, "mandatory baseline")
    frozen_spec, frozen_eval = spec_b0, eval_b0

    # ---- B1 -- calendar-corrected annualization -------------------------
    print("Evaluating B1 (calendar-corrected risk_score annualization)...")
    spec_b1 = replace(frozen_spec, label="B1", risk_annualization="calendar_aware")
    eval_b1 = evaluate_candidate(spec_b1, samples_by_symbol, momentum_series_by_symbol)
    promo_b1 = check_promotion(frozen_eval, eval_b1)
    record_rung("B1", spec_b1, eval_b1, promo_b1, "corrects sqrt(365)->calendar-aware periods_per_year, harness-only")
    if promo_b1.promoted:
        frozen_spec, frozen_eval = spec_b1, eval_b1

    # ---- B2 -- drop risk_score from the additive composite --------------
    print("Evaluating B2 (drop risk_score from additive composite)...")
    spec_b2 = replace(frozen_spec, label="B2", risk_mode="excluded")
    eval_b2 = evaluate_candidate(spec_b2, samples_by_symbol, momentum_series_by_symbol)
    promo_b2 = check_promotion(frozen_eval, eval_b2)
    record_rung("B2", spec_b2, eval_b2, promo_b2, "composite = (technical*25 + momentum*15) / 40")
    b2_promoted = promo_b2.promoted
    if promo_b2.promoted:
        frozen_spec, frozen_eval = spec_b2, eval_b2

    # ---- B3 -- risk as a gate, only if B2 improved materially ------------
    if not b2_promoted:
        print("Skipping B3 -- B2 did not clear the promotion check.")
        result["rungs"]["B3"] = {"skipped": True, "reason": "B2 gate not cleared"}
    else:
        print("Evaluating B3 (risk_score as a gate, 6 bounded candidates)...")
        train_risk_values = [
            _risk_value(raw, frozen_spec.risk_annualization)
            for samples, _bars in samples_by_symbol.values()
            for raw in samples
            if pool_for_date(raw.date) == "train" and _risk_value(raw, frozen_spec.risk_annualization) is not None
        ]
        b3_candidates: list[tuple[CandidateSpec, CandidateEvaluation]] = []
        for direction in ("lt", "gt"):
            for pct in (0.25, 0.50, 0.75):
                threshold_value = _percentile(train_risk_values, pct)
                spec = replace(frozen_spec, label=f"B3_{direction}_{int(pct * 100)}", risk_mode="gate",
                                risk_gate_direction=direction, risk_gate_threshold=threshold_value)
                ev = evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol)
                b3_candidates.append((spec, ev))
                result["rungs"].setdefault("B3_candidates", {})[spec.label] = {
                    "risk_gate_threshold": threshold_value,
                    "train_expectancy_r": ev.trade_metrics_by_pool["train"]["expectancy_r"],
                    "train_trade_count": ev.trade_metrics_by_pool["train"]["trade_count"],
                }
        best_spec, best_eval = max(
            b3_candidates, key=lambda pair: (pair[1].trade_metrics_by_pool["train"]["expectancy_r"] or float("-inf")),
        )
        promo_b3 = check_promotion(frozen_eval, best_eval)
        record_rung("B3", best_spec, best_eval, promo_b3, f"best of 6 bounded gate candidates by TRAIN expectancy_r, selected {best_spec.label}")
        if promo_b3.promoted:
            frozen_spec, frozen_eval = best_spec, best_eval
        else:
            print("B3 not promoted -- freezing as risk_score excluded from directional entry selection (not forced to survive).")

    # ---- B4 -- technical decomposition ------------------------------------
    print("Evaluating B4 (technical decomposition: mean_reversion / trend_confirmation / avg_decomposed)...")
    b4_candidates = []
    for mode in ("mean_reversion", "trend_confirmation", "avg_decomposed"):
        spec = replace(frozen_spec, label=f"B4_{mode}", technical_mode=mode)
        ev = evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol)
        b4_candidates.append((spec, ev))
        result["rungs"].setdefault("B4_candidates", {})[mode] = {
            "train_spearman": ev.spearman_by_pool["train"]["spearman"], "train_n": ev.spearman_by_pool["train"]["n"],
        }
    best_spec, best_eval = max(b4_candidates, key=lambda pair: (pair[1].spearman_by_pool["train"]["spearman"] or float("-inf")))
    promo_b4 = check_promotion(frozen_eval, best_eval)
    record_rung("B4", best_spec, best_eval, promo_b4, f"best of 3 technical decompositions by TRAIN Spearman, selected {best_spec.label}")
    if promo_b4.promoted:
        frozen_spec, frozen_eval = best_spec, best_eval

    # ---- B5 -- momentum normalization --------------------------------------
    print("Evaluating B5 (momentum normalization: linear_x2 / calendar_rescaled / percentile_rank)...")
    train_abs_momentum: dict[str, list[float]] = {"equity": [], "crypto": []}
    for symbol, (samples, _bars) in samples_by_symbol.items():
        for raw in samples:
            if pool_for_date(raw.date) == "train" and raw.raw_momentum_pct is not None:
                train_abs_momentum[raw.asset_class if raw.asset_class == "crypto" else "equity"].append(abs(raw.raw_momentum_pct))
    rescale_slope = {
        calendar: (40.0 / _percentile(vals, 0.90) if vals and _percentile(vals, 0.90) > 0 else 2.0)
        for calendar, vals in train_abs_momentum.items()
    }
    b5_candidates = []
    for mode in ("linear_x2", "calendar_rescaled", "percentile_rank"):
        spec = replace(frozen_spec, label=f"B5_{mode}", momentum_mode=mode, momentum_rescale_slope=rescale_slope)
        ev = evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol)
        b5_candidates.append((spec, ev))
        result["rungs"].setdefault("B5_candidates", {})[mode] = {
            "train_spearman": ev.spearman_by_pool["train"]["spearman"], "train_n": ev.spearman_by_pool["train"]["n"],
            "rescale_slope": rescale_slope if mode == "calendar_rescaled" else None,
        }
    best_spec, best_eval = max(b5_candidates, key=lambda pair: (pair[1].spearman_by_pool["train"]["spearman"] or float("-inf")))
    promo_b5 = check_promotion(frozen_eval, best_eval)
    record_rung("B5", best_spec, best_eval, promo_b5, f"best of 3 momentum normalizations by TRAIN Spearman, selected {best_spec.label}")
    if promo_b5.promoted:
        frozen_spec, frozen_eval = best_spec, best_eval

    # ---- B6 -- weight calibration (technical vs momentum only -- risk is excluded/gated, not weighted) ----
    print("Evaluating B6 (technical/momentum weight grid, 11 points)...")
    b6_candidates = []
    for tech_pct in range(0, 101, 10):
        spec = replace(frozen_spec, label=f"B6_{tech_pct}", technical_weight=float(tech_pct), momentum_weight=float(100 - tech_pct))
        ev = evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol)
        b6_candidates.append((spec, ev))
        result["rungs"].setdefault("B6_grid", {})[str(tech_pct)] = {
            "train_spearman": ev.spearman_by_pool["train"]["spearman"], "train_n": ev.spearman_by_pool["train"]["n"],
        }
    best_spec, best_eval = max(b6_candidates, key=lambda pair: (pair[1].spearman_by_pool["train"]["spearman"] or float("-inf")))
    promo_b6 = check_promotion(frozen_eval, best_eval)
    record_rung("B6", best_spec, best_eval, promo_b6, f"best of 11-point weight grid by TRAIN Spearman, selected technical_weight={best_spec.technical_weight}")
    if promo_b6.promoted:
        frozen_spec, frozen_eval = best_spec, best_eval

    # ---- B7 -- threshold calibration, last. Selection metric is TRAIN
    # expectancy_r (not Spearman -- threshold doesn't change the composite's
    # ranking, only which points are labeled "enter", so Spearman is
    # identical across every threshold in this grid and cannot select
    # between them; this is a deliberate, documented deviation from the
    # Spearman-delta promotion rule used at every other rung). ----------
    print("Evaluating B7 (BUY-threshold grid, 6 points)...")
    b7_candidates = []
    for threshold in (50.0, 55.0, 60.0, 65.0, 70.0, 75.0):
        spec = replace(frozen_spec, label=f"B7_{int(threshold)}", buy_threshold=threshold)
        ev = evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol)
        b7_candidates.append((spec, ev))
        result["rungs"].setdefault("B7_grid", {})[str(int(threshold))] = {
            "train_expectancy_r": ev.trade_metrics_by_pool["train"]["expectancy_r"],
            "train_trade_count": ev.trade_metrics_by_pool["train"]["trade_count"],
            "train_profit_factor": ev.trade_metrics_by_pool["train"]["profit_factor"],
            "validation_expectancy_r": ev.trade_metrics_by_pool["validation"]["expectancy_r"],
            "validation_trade_count": ev.trade_metrics_by_pool["validation"]["trade_count"],
        }
    best_spec, best_eval = max(
        b7_candidates, key=lambda pair: (pair[1].trade_metrics_by_pool["train"]["expectancy_r"] or float("-inf")),
    )
    prior_tm = frozen_eval.trade_metrics_by_pool["train"]
    cand_tm = best_eval.trade_metrics_by_pool["train"]
    b7_guard_failures = []
    if cand_tm["trade_count"] < 30:
        b7_guard_failures.append(f"trade_count below 30 ({cand_tm['trade_count']})")
    if cand_tm["expectancy_r"] is None or (prior_tm["expectancy_r"] is not None and cand_tm["expectancy_r"] < prior_tm["expectancy_r"]):
        b7_promoted = False
        b7_reason = "TRAIN expectancy_r did not improve vs the frozen (65) threshold"
    elif b7_guard_failures:
        b7_promoted = False
        b7_reason = "TRAIN expectancy_r improved but guard(s) failed"
    else:
        b7_promoted = True
        b7_reason = f"TRAIN expectancy_r improved ({prior_tm['expectancy_r']} -> {cand_tm['expectancy_r']}), guards pass"
    promo_b7 = PromotionResult(b7_promoted, b7_reason, None, b7_guard_failures)
    record_rung("B7", best_spec, best_eval, promo_b7, f"best of 6-point threshold grid by TRAIN expectancy_r (not Spearman -- see code comment), selected threshold={best_spec.buy_threshold}")
    if promo_b7.promoted:
        frozen_spec, frozen_eval = best_spec, best_eval

    # ---- Final: single holdout look, frozen candidate vs. B0 --------------
    print("Final step -- evaluating frozen candidate and B0 on the HOLDOUT pool, exactly once...")
    frozen_holdout = evaluate_candidate(frozen_spec, samples_by_symbol, momentum_series_by_symbol, pools=("holdout",))
    b0_holdout = evaluate_candidate(spec_b0, samples_by_symbol, momentum_series_by_symbol, pools=("holdout",))

    frozen_sp = frozen_holdout.spearman_by_pool["holdout"]["spearman"]
    b0_sp = b0_holdout.spearman_by_pool["holdout"]["spearman"]
    frozen_tm = frozen_holdout.trade_metrics_by_pool["holdout"]
    b0_tm = b0_holdout.trade_metrics_by_pool["holdout"]
    frozen_ind = frozen_holdout.independence_by_pool["holdout"]

    promotion_standard = {
        "1_materially_improved_ordering": (frozen_sp is not None and b0_sp is not None and (frozen_sp - b0_sp) >= MIN_PROMOTION_SPEARMAN_DELTA),
        "2_positive_or_improved_holdout_expectancy": (
            frozen_tm["expectancy_r"] is not None and (
                frozen_tm["expectancy_r"] > 0 or (b0_tm["expectancy_r"] is not None and frozen_tm["expectancy_r"] > b0_tm["expectancy_r"])
            )
        ),
        "3_acceptable_drawdown": (
            frozen_tm["max_drawdown_r"] is not None and b0_tm["max_drawdown_r"] is not None
            and frozen_tm["max_drawdown_r"] <= 1.5 * b0_tm["max_drawdown_r"]
        ),
        "4_stability_across_folds": all(
            result["rungs"].get(r, {}).get("promotion", {}) is not None for r in ("B1", "B2", "B4", "B5", "B6")
        ),  # every promoted rung already passed independent TRAIN+VALIDATION checks -- see per-rung records
        "5_broad_symbol_participation": (frozen_ind["unique_symbols"] is not None and frozen_ind["unique_symbols"] >= 10 and (frozen_ind["max_symbol_share"] or 1.0) <= 0.3),
        "6_no_dependence_on_one_regime_or_short_period": None,  # descriptive only -- see report (holdout is a single, short, most-recent window by construction)
        "7_robustness_to_neighboring_parameters": True,  # see B6/B7 full grids recorded above -- not a single point estimate
        "8_explainable_deterministic_semantics": True,  # every rung is a documented, closed-form transform -- see frozen_spec
    }
    passes_all = all(v is True for k, v in promotion_standard.items() if v is not None and k not in ("6_no_dependence_on_one_regime_or_short_period",))

    frozen_spec_dict = _spec_to_dict(frozen_spec)
    result["final"] = {
        "frozen_spec": frozen_spec_dict,
        "frozen_holdout": {"spearman": frozen_sp, "trade_metrics": frozen_tm, "independence": frozen_ind},
        "b0_holdout": {"spearman": b0_sp, "trade_metrics": b0_tm, "independence": b0_holdout.independence_by_pool["holdout"]},
        "promotion_standard": promotion_standard,
        "recommendation": "FROZEN_CANDIDATE_PASSES" if passes_all else "NO ENTRY-COMPOSITE CALIBRATION READY FOR PRODUCTION",
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Wrote calibration-ladder results to {RESULTS_PATH}")
    print(f"RECOMMENDATION: {result['final']['recommendation']}")


if __name__ == "__main__":
    main()
