"""Read-only cross-sectional out-of-universe check for the entry-composite
calibration ladder (see docs/entry-composite-calibration-ladder.md).

That ladder's own conclusion was `NEW CALIBRATION GENERATION REQUIRED`: the
Generation-2 freeze (docs/generation-2-calibration-freeze.md) established
that every cached bar through 2026-09-04 has already been viewed by some
calibration tool in this lineage, so no *temporal* holdout remains until
enough new market days accumulate strictly after that freeze date.

This script asks a different, immediately-answerable question instead: does
B6 (the ladder's one candidate that cleared TRAIN+VALIDATION -- technical
weight raised to 100, momentum to 0, risk_score left additive/uncalibrated,
threshold unchanged at 65) hold up on symbols that have NEVER been loaded by
ANY script in this calibration lineage (fetch_alpaca_history.py,
calibrate_exit_params.py, diagnose_signal_sparsity.py,
entry_composite_audit.py, entry_calibration_ladder.py all iterate strictly
over tradepulse.strategy.universe.DEFAULT_EQUITY_UNIVERSE/
DEFAULT_CRYPTO_UNIVERSE) -- a cross-sectional out-of-sample check (new
symbols, old calendar dates) rather than a temporal one (old symbols, new
dates). This is weaker evidence than a genuine future-dated holdout and
does NOT unfreeze Generation-2 or promote anything -- it is a supplementary,
honestly-labeled data point only.

Hard constraints (same as every script in this series): no tradepulse/ file
is touched; no production weight/threshold/formula changes; no commit/push.
Fetched bars are cached under data/calibration/{equity,crypto}/ using
fetch_alpaca_history.py's own _fetch_and_cache -- same cache format, new
symbol names, never overwriting an in-universe file.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOL_DIR))

from tradepulse.broker import AlpacaClient  # noqa: E402
from tradepulse.config import Settings  # noqa: E402
from tradepulse.strategy.universe import DEFAULT_CRYPTO_UNIVERSE, DEFAULT_EQUITY_UNIVERSE  # noqa: E402
import entry_calibration_ladder as ecl  # noqa: E402
from entry_composite_audit import spearman  # noqa: E402
from fetch_alpaca_history import _fetch_and_cache, _load_dotenv  # noqa: E402
from simulate_trades import CACHE_ROOT  # noqa: E402

RESULTS_PATH = CACHE_ROOT / "out_of_universe_check.json"

# Deliberately disjoint from DEFAULT_EQUITY_UNIVERSE/DEFAULT_CRYPTO_UNIVERSE
# (tradepulse/strategy/universe.py) -- verified programmatically below
# before any fetch happens, so this can never silently re-use an
# already-contaminated symbol.
OOD_EQUITY_UNIVERSE = (
    "NFLX", "AMD", "INTC", "ORCL", "CRM", "ADBE", "CSCO",   # tech, not in the mega-cap-7
    "DIS", "PEP", "COST", "MCD",                             # consumer blue chips
    "GS", "MS", "WFC",                                       # financials (JPM/BAC/V/MA already in-universe)
    "PFE", "ABBV",                                            # healthcare (JNJ/UNH already in-universe)
    "XOM", "CVX",                                             # energy (no energy name in-universe at all)
)
OOD_CRYPTO_CANDIDATES = ("AVAX/USD", "DOGE/USD", "LINK/USD", "UNI/USD", "DOT/USD")

# The frozen ladder candidate, copied verbatim from
# data/calibration/entry_calibration_ladder.json's recorded "B6" rung --
# never re-derived by hand, so this can't silently drift from what the
# report actually recommends.
FROZEN_B6_SPEC = ecl.CandidateSpec(
    label="B6_100", risk_annualization="uncalibrated", risk_mode="additive",
    technical_mode="blended", momentum_mode="linear_x2",
    technical_weight=100.0, momentum_weight=0.0, risk_weight=15.0, buy_threshold=65.0,
)
B0_SPEC = ecl.CandidateSpec(label="B0")


def _assert_disjoint_from_production_universe(symbols: tuple[str, ...]) -> None:
    overlap = set(symbols) & (set(DEFAULT_EQUITY_UNIVERSE) | set(DEFAULT_CRYPTO_UNIVERSE))
    if overlap:
        raise SystemExit(f"REFUSING TO RUN: OOD universe overlaps production universe: {overlap}")


async def _fetch_ood_data(refresh: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _load_dotenv()
    settings = Settings.from_env()
    client = AlpacaClient(settings.alpaca_api_key, settings.alpaca_api_secret, "paper", 30, equity_feed="iex")
    fetched_crypto: list[str] = []
    try:
        print("Fetching OOD equities...")
        for symbol in OOD_EQUITY_UNIVERSE:
            await _fetch_and_cache(client, symbol, "equity", refresh)
        print("Fetching OOD crypto candidates (some may not be supported by Alpaca -- kept only if bars come back)...")
        for symbol in OOD_CRYPTO_CANDIDATES:
            await _fetch_and_cache(client, symbol, "crypto", refresh)
            path = CACHE_ROOT / "crypto" / f"{symbol.replace('/', '-')}.json"
            if path.exists() and json.loads(path.read_text())["bar_count"] > 0:
                fetched_crypto.append(symbol)
            else:
                print(f"  {symbol}: no bars returned, dropping from OOD crypto set")
    finally:
        await client.aclose()
    return OOD_EQUITY_UNIVERSE, tuple(fetched_crypto)


def _evaluate_ood(spec: ecl.CandidateSpec, samples_by_symbol: dict) -> dict:
    """Reuses entry_calibration_ladder.evaluate_candidate's exact,
    already-tested entry/exit/Spearman logic (25 passing tests cover it)
    rather than re-deriving trade simulation here. That function filters by
    `pool_for_date` (train/validation/holdout by calendar date), which is
    meaningless for this cross-sectional check -- every OOD sample is
    equally "unseen" regardless of its date, since the SYMBOL itself, not
    the date, is what was never loaded by any prior tool. Monkeypatched for
    the duration of this call only, restored immediately after."""
    original_pool_for_date = ecl.pool_for_date
    ecl.pool_for_date = lambda _d: "ood"
    try:
        return ecl.evaluate_candidate(spec, samples_by_symbol, momentum_series_by_symbol={}, pools=("ood",))
    finally:
        ecl.pool_for_date = original_pool_for_date


def main() -> None:
    refresh = "--refresh" in sys.argv
    _assert_disjoint_from_production_universe(OOD_EQUITY_UNIVERSE + OOD_CRYPTO_CANDIDATES)

    equities, crypto = asyncio.run(_fetch_ood_data(refresh))
    print(f"OOD equities ({len(equities)}): {equities}")
    print(f"OOD crypto ({len(crypto)}, Alpaca-confirmed): {crypto}")

    print("Generating ladder samples for OOD symbols (reusing production's exact factor pipeline)...")
    samples_by_symbol: dict[str, tuple[list, list]] = {}
    for asset_class, universe, benchmark_symbol in (
        ("equity", equities, "SPY"), ("crypto", crypto, "BTC/USD"),
    ):
        if not universe:
            continue
        bench_bars = json.loads((CACHE_ROOT / asset_class / f"{benchmark_symbol.replace('/', '-')}.json").read_text())["bars"]
        for symbol in universe:
            samples, bars = ecl.generate_ladder_samples(symbol, asset_class, bench_bars)
            samples_by_symbol[symbol] = (samples, bars)
            print(f"  {symbol}: {len(samples)} raw samples")

    def _segment(spec_label: str, subset: dict) -> dict:
        eval_b0 = _evaluate_ood(B0_SPEC, subset)
        eval_b6 = _evaluate_ood(FROZEN_B6_SPEC, subset)
        b0_sp, b6_sp = eval_b0.spearman_by_pool["ood"], eval_b6.spearman_by_pool["ood"]
        b0_tm, b6_tm = eval_b0.trade_metrics_by_pool["ood"], eval_b6.trade_metrics_by_pool["ood"]
        b0_ind, b6_ind = eval_b0.independence_by_pool["ood"], eval_b6.independence_by_pool["ood"]
        print(f"-- {spec_label} --")
        print(f"  B0 spearman: n={b0_sp['n']}, rho={b0_sp['spearman']}")
        print(f"  B6 spearman: n={b6_sp['n']}, rho={b6_sp['spearman']}")
        print(f"  B0 trades:   {b0_tm}")
        print(f"  B6 trades:   {b6_tm}")
        return {
            "b0": {"spearman": b0_sp, "trade_metrics": b0_tm, "independence": b0_ind},
            "b6_frozen_candidate": {"spearman": b6_sp, "trade_metrics": b6_tm, "independence": b6_ind},
            "spearman_delta_b6_minus_b0": (
                (b6_sp["spearman"] - b0_sp["spearman"]) if b6_sp["spearman"] is not None and b0_sp["spearman"] is not None else None
            ),
            "expectancy_delta_b6_minus_b0": (
                (b6_tm["expectancy_r"] - b0_tm["expectancy_r"]) if b6_tm["expectancy_r"] is not None and b0_tm["expectancy_r"] is not None else None
            ),
        }

    equity_only = {s: v for s, v in samples_by_symbol.items() if v[0] and v[0][0].asset_class == "equity"}
    crypto_only = {s: v for s, v in samples_by_symbol.items() if v[0] and v[0][0].asset_class == "crypto"}

    print("Evaluating on the COMBINED OOD basket (equity + crypto pooled)...")
    combined = _segment("combined", samples_by_symbol)
    print("Evaluating on OOD EQUITY ONLY (apples-to-apples with the original per-asset-class audit)...")
    equity_result = _segment("equity_only", equity_only)
    print("Evaluating on OOD CRYPTO ONLY...")
    crypto_result = _segment("crypto_only", crypto_only)

    result = {
        "purpose": "cross-sectional (new symbols, old dates) supplementary check -- NOT a substitute for a genuine "
                   "temporal Generation-2 holdout, and not a promotion decision. Reported per-asset-class as well as "
                   "combined, matching the original forensic audit's own per-asset-class discipline.",
        "ood_equity_universe": list(equities), "ood_crypto_universe": list(crypto),
        "combined": combined, "equity_only": equity_result, "crypto_only": crypto_result,
    }
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote OOD check results to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
