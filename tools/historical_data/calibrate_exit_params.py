"""Orchestrates the exit-parameter calibration: generates entries once per
symbol (cached), runs the break_even_trigger_pct x max_hold_days grid over
common-calendar walk-forward folds, applies frozen friction scenarios, and
writes the raw aggregate results as JSON for the report generator to consume.

This script produces DATA ONLY (data/calibration/results.json) -- it never
writes to tradepulse/ and never selects/applies a "winning" parameter value
itself. See docs/exit-parameter-calibration.md (generated separately by
render_report.py) for the actual recommendation.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOL_DIR))

from tradepulse.config import risk_limits_for_profile  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
from simulate_trades import CACHE_ROOT, Entry, TradeOutcome, generate_entries, simulate_exit  # noqa: E402

RESULTS_PATH = CACHE_ROOT / "results.json"
ENTRIES_CACHE_PATH = CACHE_ROOT / "entries.json"

# Held fixed throughout the grid -- balanced's already-calibrated value, the
# middle of the four profiles (2.0-3.0) -- this pass tests
# break_even_trigger_pct/max_hold_days only, not trailing_atr_multiplier.
TRAILING_ATR_MULTIPLIER = risk_limits_for_profile("balanced").trailing_atr_multiplier

BREAK_EVEN_GRID = [Decimal(v) for v in ("2", "2.5", "3", "4", "6", "8")]
MAX_HOLD_DAYS_GRID = [5, 10, 12, 15, 20, 30, 45, 60]

FOLDS = [
    {"name": "fold_1", "train_end": date(2022, 12, 31), "test_start": date(2023, 1, 1), "test_end": date(2023, 12, 31)},
    {"name": "fold_2", "train_end": date(2023, 12, 31), "test_start": date(2024, 1, 1), "test_end": date(2024, 12, 31)},
    {"name": "fold_3", "train_end": date(2024, 12, 31), "test_start": date(2025, 1, 1), "test_end": date(2025, 12, 31)},
    {"name": "fold_4", "train_end": date(2025, 8, 31), "test_start": date(2025, 9, 1), "test_end": date(2099, 1, 1)},
]

# Frozen BEFORE any result is examined -- see docs/exit-parameter-calibration.md's methodology section.
FRICTION_BPS = {
    "equity": {"gross": Decimal("0"), "base": Decimal("0.05"), "stress": Decimal("0.15")},
    "crypto": {"gross": Decimal("0"), "base": Decimal("0.10"), "stress": Decimal("0.30")},
}


def _load_or_generate_all_entries() -> dict[str, tuple[list[Entry], list[dict]]]:
    """symbol -> (entries, all_bars). Regenerated each run (entry generation
    is deterministic and fast relative to the grid sweep; re-fetching from
    disk cache avoids the complexity of serializing Entry/Candle objects
    round-trip)."""
    result: dict[str, tuple[list[Entry], list[dict]]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", DEFAULT_EQUITY_UNIVERSE, "SPY"),
        ("crypto", DEFAULT_CRYPTO_UNIVERSE, "BTC/USD"),
    ):
        bench_path = CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json"
        benchmark_bars = json.loads(bench_path.read_text(encoding="utf-8"))["bars"]
        for symbol in universe:
            entries, bars = generate_entries(symbol, asset_class, benchmark_bars)
            result[symbol] = (entries, bars)
            print(f"  {symbol}: {len(entries)} hypothetical entries")
    return result


def _apply_friction(entry: Entry, outcome: TradeOutcome, friction_bps: Decimal) -> Decimal | None:
    if outcome.r_multiple is None or outcome.exit_price is None:
        return None
    if friction_bps == 0:
        return outcome.r_multiple
    adjusted_entry = entry.entry_price * (1 + friction_bps / 100)
    adjusted_exit = outcome.exit_price * (1 - friction_bps / 100)
    risk = entry.entry_price - entry.initial_stop
    if risk <= 0:
        return Decimal("0")
    return (adjusted_exit - adjusted_entry) / risk


def _metrics(rs: list[Decimal]) -> dict:
    if not rs:
        return {"trade_count": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    expectancy = sum(rs, Decimal("0")) / len(rs)
    profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else None
    # Max drawdown of the sequential (chronological) cumulative-R equity curve.
    cum = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trade_count": len(rs),
        "hit_rate": len(wins) / len(rs),
        "expectancy_r": float(expectancy),
        "profit_factor": profit_factor,
        "avg_r": float(expectancy),
        "max_drawdown_r": float(max_dd),
    }


def _independence_metrics(entries_in_window: list[Entry]) -> dict:
    unique_symbols = len({e.symbol for e in entries_in_window})
    unique_dates = len({e.entry_date for e in entries_in_window})
    per_symbol_counts: dict[str, int] = {}
    for e in entries_in_window:
        per_symbol_counts[e.symbol] = per_symbol_counts.get(e.symbol, 0) + 1
    max_concentration = max(per_symbol_counts.values()) if per_symbol_counts else 0
    avg_concentration = (sum(per_symbol_counts.values()) / len(per_symbol_counts)) if per_symbol_counts else 0
    return {
        "unique_symbols": unique_symbols, "unique_entry_dates": unique_dates,
        "max_entries_per_symbol": max_concentration, "avg_entries_per_symbol": avg_concentration,
    }


def main() -> None:
    print("Generating entries (no-lookahead, fixed-baseline composite)...")
    all_data = _load_or_generate_all_entries()

    results: list[dict] = []
    for asset_class in ("equity", "crypto"):
        symbols = [s for s, (entries, _) in all_data.items() if entries and entries[0].asset_class == asset_class]
        for fold in FOLDS:
            train_entries = [(sym, e) for sym in symbols for e in all_data[sym][0] if date.fromisoformat(e.entry_date) <= fold["train_end"]]
            test_entries = [
                (sym, e) for sym in symbols for e in all_data[sym][0]
                if fold["test_start"] <= date.fromisoformat(e.entry_date) <= fold["test_end"]
            ]
            print(f"{asset_class} {fold['name']}: {len(train_entries)} train entries, {len(test_entries)} test entries")

            for be_pct in BREAK_EVEN_GRID:
                for max_hold in MAX_HOLD_DAYS_GRID:
                    for window_name, window_entries in (("train", train_entries), ("test", test_entries)):
                        outcomes = [
                            (e, simulate_exit(e, all_data[sym][1], be_pct, max_hold, TRAILING_ATR_MULTIPLIER))
                            for sym, e in window_entries
                        ]
                        entries_only = [e for e, _o in outcomes]
                        for friction_name, friction_val in FRICTION_BPS[asset_class].items():
                            rs = [r for e, o in outcomes if (r := _apply_friction(e, o, friction_val)) is not None]
                            censored = sum(1 for _e, o in outcomes if o.exit_reason == "censored")
                            row = {
                                "asset_class": asset_class, "fold": fold["name"], "window": window_name,
                                "break_even_trigger_pct": str(be_pct), "max_hold_days": max_hold,
                                "friction_scenario": friction_name,
                                "censored_count": censored,
                                **_metrics(rs),
                                **(_independence_metrics(entries_only) if friction_name == "gross" else {}),
                            }
                            results.append(row)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {len(results)} result rows to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
