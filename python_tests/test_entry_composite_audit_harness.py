"""Validation gate for the entry-composite forensic audit harness
(tools/historical_data/entry_composite_audit.py) -- proves the NEW
computational primitives this phase adds are correct on known small
inputs, mirroring test_calibration_harness.py's role for the prior
phase's new primitives. Does not re-test already-covered production code
(compute_real_factors/weighted_composite/etc. have their own tests) or
diagnose_signal_sparsity.py's primitives (covered when that script was
added).
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

from diagnose_signal_sparsity import Sample  # noqa: E402
from entry_composite_audit import (  # noqa: E402
    MIN_INTERACTION_CELL, correlation_table, decile_table, interaction_table, spearman, tag_regimes,
)


def _sample(
    *, symbol: str = "X", asset_class: str = "equity", d: str = "2024-01-01", index: int = 0,
    technical: float = 50.0, momentum: float = 50.0, risk: float = 50.0, composite: float = 50.0,
    relative_strength: float | None = None,
) -> Sample:
    return Sample(
        symbol=symbol, asset_class=asset_class, date=d, index=index,
        technical_score=Decimal(str(technical)), momentum_score=Decimal(str(momentum)), risk_score=Decimal(str(risk)),
        liquidity_score=Decimal("50"), risk_quality_score=Decimal("50"),
        relative_strength_score=None if relative_strength is None else Decimal(str(relative_strength)),
        composite=Decimal(str(composite)), signal="HOLD",
    )


def _fm(ret: float, mfe: float | None = None, mae: float | None = None, horizon: int = 5) -> dict:
    return {horizon: {"return": ret, "mfe": mfe if mfe is not None else max(0.0, ret), "mae": mae if mae is not None else min(0.0, ret)}}


# ---- spearman ---------------------------------------------------------------------------------


def test_spearman_perfect_positive_monotonic_relationship_is_one() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_perfect_negative_monotonic_relationship_is_minus_one() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [50.0, 40.0, 30.0, 20.0, 10.0]
    assert spearman(xs, ys) == pytest.approx(-1.0)


def test_spearman_handles_ties_via_average_ranks() -> None:
    # x has a tie at positions 0/1 (both value 1) -- average rank 1.5 each,
    # not an arbitrary tie-break order that would distort the correlation.
    xs = [1.0, 1.0, 2.0, 3.0]
    ys = [10.0, 10.0, 20.0, 30.0]
    assert spearman(xs, ys) == pytest.approx(1.0)  # still perfectly monotonic once ties are averaged consistently on both sides


def test_spearman_is_none_for_a_constant_series() -> None:
    # Every x is identical -- rank variance is zero, so the correlation is
    # genuinely undefined, not zero (which would falsely claim "no relationship").
    assert spearman([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


def test_spearman_is_none_for_fewer_than_two_points() -> None:
    assert spearman([1.0], [1.0]) is None
    assert spearman([], []) is None


def test_correlation_table_excludes_samples_missing_that_horizon() -> None:
    records = [
        (_sample(technical=10), _fm(0.01, horizon=5)),
        (_sample(technical=90), _fm(0.05, horizon=5)),
        (_sample(technical=50), {}),  # censored at h5 -- must be excluded, not treated as 0
    ]
    table = correlation_table(records, "technical_score")
    assert table["h5"]["n"] == 2


# ---- decile_table -------------------------------------------------------------------------------


def test_decile_table_splits_ascending_scores_into_ten_roughly_equal_buckets() -> None:
    records = [(_sample(technical=float(i)), _fm(float(i) / 100)) for i in range(100)]
    table = decile_table(records, "technical_score")
    assert len(table) == 10
    assert table["decile_1"]["n"] == 10
    assert table["decile_1"]["h5"]["avg_return"] < table["decile_10"]["h5"]["avg_return"]  # monotonic input stays ordered


def test_decile_table_empty_when_factor_is_always_none() -> None:
    records = [(_sample(relative_strength=None), _fm(0.01)) for _ in range(20)]
    assert decile_table(records, "relative_strength_score") == {}


# ---- interaction_table ---------------------------------------------------------------------------


def test_interaction_table_marks_thin_cells_insufficient_not_a_precise_average() -> None:
    # Only 3 samples total -- every populated cell is far below MIN_INTERACTION_CELL.
    records = [
        (_sample(momentum=10, technical=10), _fm(0.01)),
        (_sample(momentum=50, technical=50), _fm(0.02)),
        (_sample(momentum=90, technical=90), _fm(0.03)),
    ]
    table = interaction_table(records, "momentum_score", "technical_score")
    assert all(cell.get("insufficient_sample") for cell in table.values())


def test_interaction_table_computes_averages_once_a_cell_clears_the_guard() -> None:
    # Identical scores on both axes -- every sample carries the same outcome,
    # so regardless of exactly how the tie is split into rank tertiles, every
    # populated cell's average must equal that outcome and the populated
    # cells' counts must sum to the full sample.
    n = MIN_INTERACTION_CELL * 3 + 5
    records = [(_sample(momentum=1.0, technical=1.0), _fm(0.02)) for _ in range(n)]
    table = interaction_table(records, "momentum_score", "technical_score")
    populated = [c for c in table.values() if not c.get("insufficient_sample")]
    assert len(populated) >= 1
    assert sum(c["n"] for c in populated) == n
    assert all(c["avg_return"] == pytest.approx(0.02) for c in populated)


# ---- tag_regimes: as-of discipline -----------------------------------------------------------------


def _bench_bar(day: str, close: float) -> dict:
    return {"date": day, "open": str(close), "high": str(close), "low": str(close), "close": str(close), "volume": "1000"}


def test_tag_regimes_never_lets_a_future_benchmark_bar_affect_an_earlier_samples_regime() -> None:
    # A calm, flat benchmark through day 60, then a violent crash afterward.
    # A sample dated at day 60 must be classified using ONLY the calm prefix
    # -- if a future bar leaked in, both classifications below would agree
    # (both would see the crash); they must NOT agree.
    calm_days = [_bench_bar(f"2024-01-{i + 1:02d}" if i < 31 else f"2024-02-{i - 30:02d}", 100.0) for i in range(60)]
    crash_days = [_bench_bar(f"2024-03-{i + 1:02d}", 100.0 - i * 5.0) for i in range(20)]
    full_bench = calm_days + crash_days

    sample_at_day_60 = _sample(asset_class="equity", d=calm_days[-1]["date"], composite=50)
    flat = {"equity": [(sample_at_day_60, _fm(0.0))]}

    tagged_without_future = tag_regimes(flat, {"equity": calm_days})
    tagged_with_future_present = tag_regimes(flat, {"equity": full_bench})

    regime_without_future = tagged_without_future["equity"][0][2]
    regime_with_future_present = tagged_with_future_present["equity"][0][2]
    assert regime_without_future == regime_with_future_present  # as-of lookup makes the extra future rows irrelevant
