"""Read-only empirical evidence on whether a fixed take-profit target would
help TradePulse's current pure trailing-stop/break-even/time-stop exit
policy -- see docs/target-price-evidence.md for the full report.

This is NOT a calibration or promotion pass, and proposes no production
change: `target_price` is confirmed intentional architecture (fully wired
through models/execution/settlement/monitor, but never computed anywhere in
scanner/coordinator.py for any asset class -- see docs/ discussion), and
adding real take-profit behavior would be a strategy-design change, not a
bug fix. Per the same evidence-before-guessing discipline as every other
calibration tool in this directory (Generation-2 freeze, held-out ladder),
this script answers the descriptive questions that should be asked BEFORE
picking any target-sizing method or reward:risk multiple, rather than
guessing one:

  1. How often does price reach 1R/1.5R/2R/2.5R/3R (R = entry price minus
     the ATR-based initial stop, i.e. the same "1R" the ladder/exit-param
     calibration already uses) before the CURRENT policy's own exit fires?
  2. Of the profit reached at each milestone, how much does the current
     policy end up giving back by the time it actually exits?
  3. How would a hard, full-exit take-profit target at each R candidate
     compare against the current trailing/break-even/time-stop policy on
     the exact same walk-forward folds already approved for exit-parameter
     calibration (calibrate_exit_params.FOLDS) and the same frozen friction
     scenarios?

Reuses simulate_trades.py's entry generation and exit simulation directly
(same fixed-baseline composite, same ATR stop, same no-lookahead
machinery) -- no new entry logic, no new data fetch. No AI-recommendation
gate is simulated, same permanent limitation as every other tool here.
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

from tradepulse.config import risk_limits_for_profile  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
from calibrate_exit_params import FOLDS, FRICTION_BPS, _apply_friction, _independence_metrics, _metrics  # noqa: E402
from simulate_trades import (  # noqa: E402
    ATR_TRAIL_LOOKBACK_DAYS, CACHE_ROOT, Entry, TradeOutcome, _ratchet_candidate_stop, atr, generate_entries, simulate_exit,
)

RESULTS_PATH = CACHE_ROOT / "target_price_evidence.json"

# Held fixed, same as every other script here -- balanced profile's already-
# calibrated exit parameters, decoupled from this target-only experiment.
_BALANCED = risk_limits_for_profile("balanced")
BREAK_EVEN_TRIGGER_PCT = _BALANCED.break_even_trigger_pct
MAX_HOLD_DAYS = _BALANCED.max_hold_days
TRAILING_ATR_MULTIPLIER = _BALANCED.trailing_atr_multiplier

# Descriptive milestone grid (§1/§2) -- deliberately wide, these are
# observation points, not candidates being selected between.
MILESTONE_R_GRID = (Decimal("1"), Decimal("1.5"), Decimal("2"), Decimal("2.5"), Decimal("3"))
# Policy-comparison grid (§3) -- a hard exit the instant price touches this
# many R, evaluated as a full alternative policy, not a partial scale-out
# (TradePulse has no partial-fill/scale-out mechanism today; simulating one
# here would not correspond to anything the live system could execute).
POLICY_R_GRID = (Decimal("1.5"), Decimal("2"), Decimal("2.5"), Decimal("3"))


def _risk(entry: Entry) -> Decimal:
    return entry.entry_price - entry.initial_stop


@dataclass(frozen=True, slots=True)
class MilestoneResult:
    reached: bool
    bars_to_reach: int | None
    r_at_baseline_exit: Decimal | None  # None only if baseline was censored


def _milestone_evidence(
    entry: Entry, bars: list[dict], baseline: TradeOutcome, target_r: Decimal, date_to_index: dict[str, int],
) -> MilestoneResult:
    """Walks the SAME bar range the baseline actually held the position for
    (entry_index+1 .. baseline's own exit bar, or to the end of history if
    censored) and checks whether that day's HIGH ever touched
    entry + target_r*risk -- the upside-symmetric counterpart of
    simulate_exit's own low-touches-stop convention. Never looks past the
    baseline's own exit: this answers "did price reach this milestone
    BEFORE the current policy actually exited," not "did it ever, at any
    point in the future, regardless of what already happened.\" `date_to_index`
    is precomputed once per symbol by the caller -- an O(n) linear rescan
    per (entry, target_r) pair would otherwise dominate runtime across tens
    of thousands of entries."""
    risk = _risk(entry)
    if risk <= 0:
        return MilestoneResult(False, None, None)
    target_price = entry.entry_price + risk * target_r
    end_index = date_to_index[baseline.exit_date] if baseline.exit_date is not None else len(bars) - 1
    for i in range(entry.entry_index + 1, end_index + 1):
        if Decimal(bars[i]["high"]) >= target_price:
            return MilestoneResult(True, i - entry.entry_index, baseline.r_multiple)
    return MilestoneResult(False, None, baseline.r_multiple)


def simulate_exit_with_target(
    entry: Entry, all_bars: list[dict], break_even_trigger_pct: Decimal, max_hold_days: int,
    trailing_atr_multiplier: Decimal, target_r: Decimal,
) -> TradeOutcome:
    """Field-for-field mirror of simulate_trades.simulate_exit, with exactly
    one addition: a full exit the first day price's HIGH touches
    entry + target_r*risk. Stop is checked first on any day where both
    could theoretically fire (an extreme-range day touching both the stop
    below and the target above) -- conservative tie-break, matching general
    backtesting convention of assuming the worse outcome on genuine
    ambiguity, and consistent with simulate_exit's own stop-before-trail
    check ordering."""
    risk = _risk(entry)
    target_price = entry.entry_price + risk * target_r if risk > 0 else None
    entry_date = date.fromisoformat(entry.entry_date)
    current_stop = entry.initial_stop
    running_extreme = entry.entry_price
    ratcheted_stop: Decimal | None = None

    for i in range(entry.entry_index + 1, len(all_bars)):
        bar = all_bars[i]
        bar_date = date.fromisoformat(bar["date"])
        open_px, low_px, high_px, close_px = Decimal(bar["open"]), Decimal(bar["low"]), Decimal(bar["high"]), Decimal(bar["close"])
        running_extreme = max(running_extreme, close_px)

        operative_stop = ratcheted_stop if ratcheted_stop is not None else current_stop
        if open_px <= operative_stop:
            return TradeOutcome(entry, bar["date"], open_px, "stop", (open_px - entry.entry_price) / risk if risk > 0 else Decimal("0"))
        if low_px <= operative_stop:
            return TradeOutcome(entry, bar["date"], operative_stop, "stop", (operative_stop - entry.entry_price) / risk if risk > 0 else Decimal("0"))

        if target_price is not None:
            if open_px >= target_price:
                return TradeOutcome(entry, bar["date"], open_px, "target_price", (open_px - entry.entry_price) / risk)
            if high_px >= target_price:
                return TradeOutcome(entry, bar["date"], target_price, "target_price", target_r)

        if (bar_date - entry_date).days >= max_hold_days:
            return TradeOutcome(entry, bar["date"], close_px, "time_stop", (close_px - entry.entry_price) / risk if risk > 0 else Decimal("0"))

        gain_pct = (close_px - entry.entry_price) / entry.entry_price * 100
        atr_value: Decimal | None = None
        if i >= ATR_TRAIL_LOOKBACK_DAYS:
            window = all_bars[max(0, i - ATR_TRAIL_LOOKBACK_DAYS + 1) : i + 1]
            raw = atr([float(b["high"]) for b in window], [float(b["low"]) for b in window], [float(b["close"]) for b in window])
            atr_value = Decimal(str(raw)) if raw is not None else None
        ratcheted_stop = _ratchet_candidate_stop(
            entry.entry_price, running_extreme, atr_value, trailing_atr_multiplier, gain_pct, break_even_trigger_pct, ratcheted_stop,
        )

    return TradeOutcome(entry, None, None, "censored", None)


def main() -> None:
    print("Generating entries (identical to every prior calibration pass -- same fixed baseline, same ATR stop)...")
    all_data: dict[str, tuple[list[Entry], list[dict]]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"), ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_path = CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json"
        benchmark_bars = json.loads(bench_path.read_text(encoding="utf-8"))["bars"]
        for symbol in universe:
            entries, bars = generate_entries(symbol, asset_class, benchmark_bars)
            all_data[symbol] = (entries, bars)
            print(f"  {symbol}: {len(entries)} hypothetical entries")

    result: dict = {"milestones": {}, "policy_comparison": {}}

    # ---- §1/§2: descriptive milestone evidence -----------------------------
    print("Computing milestone-reach / give-back evidence (no policy change simulated)...")
    for asset_class in ("equity", "crypto"):
        symbols = [s for s, (entries, _) in all_data.items() if entries and entries[0].asset_class == asset_class]
        entries_flat: list[tuple[str, Entry]] = [(sym, e) for sym in symbols for e in all_data[sym][0]]
        date_to_index_by_symbol: dict[str, dict[str, int]] = {
            sym: {b["date"]: i for i, b in enumerate(all_data[sym][1])} for sym in symbols
        }
        # Baseline computed exactly once per entry (not once per target_r) --
        # simulate_exit's own result never depends on target_r, only reused.
        baselines: list[TradeOutcome] = [
            simulate_exit(e, all_data[sym][1], BREAK_EVEN_TRIGGER_PCT, MAX_HOLD_DAYS, TRAILING_ATR_MULTIPLIER)
            for sym, e in entries_flat
        ]

        milestone_stats: dict[str, dict] = {}
        for target_r in MILESTONE_R_GRID:
            reached_count = 0
            giveback_rs: list[Decimal] = []
            total = 0
            for (sym, e), baseline in zip(entries_flat, baselines):
                if baseline.r_multiple is None:
                    continue  # censored -- excluded, never padded
                total += 1
                m = _milestone_evidence(e, all_data[sym][1], baseline, target_r, date_to_index_by_symbol[sym])
                if m.reached:
                    reached_count += 1
                    giveback_rs.append(target_r - baseline.r_multiple)  # >0 means profit was given back after this milestone
            milestone_stats[str(target_r)] = {
                "total_entries": total,
                "reached_count": reached_count,
                "reached_pct": (reached_count / total) if total else None,
                "avg_giveback_r_when_reached": (float(sum(giveback_rs) / len(giveback_rs)) if giveback_rs else None),
                "pct_giving_back_more_than_0.5R": (
                    sum(1 for g in giveback_rs if g > Decimal("0.5")) / len(giveback_rs) if giveback_rs else None
                ),
            }
            print(f"  {asset_class} {target_r}R: reached {reached_count}/{total} ({milestone_stats[str(target_r)]['reached_pct']})")
        result["milestones"][asset_class] = milestone_stats

    # ---- §3: full-policy comparison, same folds/friction as calibrate_exit_params.py ----
    print("Computing full-policy comparison (hard target-exit vs. current trailing policy) on approved folds...")
    for asset_class in ("equity", "crypto"):
        symbols = [s for s, (entries, _) in all_data.items() if entries and entries[0].asset_class == asset_class]
        for fold in FOLDS:
            test_entries = [
                (sym, e) for sym in symbols for e in all_data[sym][0]
                if fold["test_start"] <= date.fromisoformat(e.entry_date) <= fold["test_end"]
            ]
            for policy_name, policy_fn in [("baseline_trailing_only", None)] + [
                (f"target_{r}R", r) for r in POLICY_R_GRID
            ]:
                outcomes = [
                    (e, simulate_exit(e, all_data[sym][1], BREAK_EVEN_TRIGGER_PCT, MAX_HOLD_DAYS, TRAILING_ATR_MULTIPLIER)
                     if policy_fn is None else
                     simulate_exit_with_target(e, all_data[sym][1], BREAK_EVEN_TRIGGER_PCT, MAX_HOLD_DAYS, TRAILING_ATR_MULTIPLIER, policy_fn))
                    for sym, e in test_entries
                ]
                entries_only = [e for e, _o in outcomes]
                for friction_name, friction_val in FRICTION_BPS[asset_class].items():
                    rs = [r for e, o in outcomes if (r := _apply_friction(e, o, friction_val)) is not None]
                    row = {
                        "asset_class": asset_class, "fold": fold["name"], "policy": policy_name, "friction_scenario": friction_name,
                        **_metrics(rs),
                        **(_independence_metrics(entries_only) if friction_name == "gross" else {}),
                    }
                    result["policy_comparison"].setdefault(f"{asset_class}__{fold['name']}__{policy_name}", {})[friction_name] = row

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Wrote target-price evidence to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
