"""Validation gate for the entry-calibration-ladder harness
(tools/historical_data/entry_calibration_ladder.py) -- proves the NEW
primitives this phase adds are correct on known small inputs: B1's
calendar-aware re-annualization arithmetic, B5's trailing percentile
momentum transform, B4's technical decomposition (must recombine to
today's blended formula when unclamped), B3's gate direction/threshold
filtering, the pool-boundary partition (TRAIN/VALIDATION/HOLDOUT never
overlap), and a structural proof that evaluate_candidate's default pool
set excludes HOLDOUT -- mirroring test_calibration_harness.py's existing
no-lookahead proof pattern for the prior phase's harness.
"""

import math
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

from entry_calibration_ladder import (  # noqa: E402
    CandidateSpec, RawSample, TRAIN_END, TRAIN_START, VALIDATION_END, VALIDATION_START, HOLDOUT_END, HOLDOUT_START,
    _risk_value, _technical_value, evaluate_candidate, pool_for_date, score_sample, trailing_percentile_momentum,
)


def _raw(
    *, symbol: str = "X", asset_class: str = "equity", d: str = "2023-06-01", index: int = 0,
    rsi: float | None = 50.0, macd_histogram: float | None = 0.0, ma50: float | None = 100.0, ma200: float | None = 100.0,
    bollinger_percent_b: float | None = 50.0, raw_momentum_pct: float | None = 0.0, raw_vol_365: float | None = 20.0,
) -> RawSample:
    return RawSample(
        symbol=symbol, asset_class=asset_class, date=d, index=index,
        rsi=rsi, macd_histogram=macd_histogram, ma50=ma50, ma200=ma200, bollinger_percent_b=bollinger_percent_b,
        raw_momentum_pct=raw_momentum_pct, raw_vol_365=raw_vol_365,
        b0_technical_score=Decimal("50"), b0_momentum_score=Decimal("50"), b0_risk_score=Decimal(str(100 - raw_vol_365)) if raw_vol_365 is not None else Decimal("50"),
        b0_composite=Decimal("50"), b0_signal="HOLD",
    )


# ---- B1: calendar-aware re-annualization -----------------------------------


def test_b1_crypto_annualization_is_unchanged_from_raw_365() -> None:
    # periods_per_year for crypto is 365 -- sqrt(365/365) == 1, so B1 must
    # be numerically identical to the raw sqrt(365) value for crypto.
    raw = _raw(asset_class="crypto", raw_vol_365=40.0)
    uncalibrated = _risk_value(raw, "uncalibrated")
    calibrated = _risk_value(raw, "calendar_aware")
    assert calibrated == pytest.approx(uncalibrated)


def test_b1_equity_annualization_matches_hand_computed_rescale() -> None:
    # raw_vol_365 = stdev * sqrt(365) * 100 (indicators.volatility's own
    # definition). Correcting to periods=252: corrected = raw * sqrt(252/365).
    raw_vol_365 = 40.0
    expected_corrected_vol = raw_vol_365 * math.sqrt(252 / 365)
    expected_risk_score = 100 - expected_corrected_vol
    raw = _raw(asset_class="equity", raw_vol_365=raw_vol_365)
    assert _risk_value(raw, "calendar_aware") == pytest.approx(expected_risk_score)


def test_b1_equity_correction_raises_risk_score_since_252_lt_365() -> None:
    # A smaller periods_per_year constant means a SMALLER annualized vol
    # number, which means a HIGHER risk_score (100 - vol) -- the correction
    # moves equity's risk_score up, not down.
    raw = _raw(asset_class="equity", raw_vol_365=40.0)
    assert _risk_value(raw, "calendar_aware") > _risk_value(raw, "uncalibrated")


def test_b1_clamps_at_zero_for_extreme_volatility() -> None:
    raw = _raw(asset_class="equity", raw_vol_365=500.0)
    assert _risk_value(raw, "calendar_aware") == 0.0


def test_risk_value_is_none_when_raw_volatility_unavailable() -> None:
    raw = _raw(raw_vol_365=None)
    assert _risk_value(raw, "uncalibrated") is None
    assert _risk_value(raw, "calendar_aware") is None


# ---- B4: technical decomposition recombines to today's blended formula -----


def test_technical_decomposition_recombines_to_blended_formula_when_unclamped() -> None:
    # Values chosen so no sub-term saturates 0/100 in any of the three
    # computations -- under that condition, mean_reversion + trend_confirmation
    # - 100 must equal the hand-computed blended formula exactly, since they
    # are the same formula (factors.py's technical_score) split into two
    # additive halves around the same 50 baseline. NOTE: `_technical_value`'s
    # "blended" mode reads the production-computed b0_technical_score field
    # directly (a real FactorScores value in production, a fixed stand-in
    # in this synthetic RawSample) -- so this test hand-computes the
    # expected blended value from the same raw sub-fields rather than
    # calling "blended" mode, which would just echo the stand-in.
    raw = _raw(rsi=40.0, macd_histogram=1.0, ma50=105.0, ma200=100.0, bollinger_percent_b=15.0)
    expected_blended = 50 + (50 - 40) * 0.5 + 10 + 10 + 8  # rsi term, macd term, ma-cross term, bollinger term
    mean_reversion = _technical_value(raw, "mean_reversion")
    trend_confirmation = _technical_value(raw, "trend_confirmation")
    # Each sub-component carries its OWN +50 baseline, so recombining both
    # double-counts the baseline once -- subtract 50 (not 100) to recover
    # the single shared baseline the blended formula uses.
    assert mean_reversion + trend_confirmation - 50 == pytest.approx(expected_blended)


def test_technical_decomposition_isolates_mean_reversion_from_trend() -> None:
    # A pure trend-favorable, mean-reversion-neutral case: mean_reversion
    # must sit at exactly 50 (its own baseline), trend_confirmation must be
    # elevated -- proving the split actually separates the two philosophies
    # rather than just relabeling the same number.
    raw = _raw(rsi=50.0, bollinger_percent_b=50.0, macd_histogram=1.0, ma50=110.0, ma200=100.0)
    assert _technical_value(raw, "mean_reversion") == pytest.approx(50.0)
    assert _technical_value(raw, "trend_confirmation") == pytest.approx(70.0)  # +10 MACD, +10 MA cross


# ---- B5: trailing percentile momentum transform -----------------------------


def test_trailing_percentile_momentum_ranks_within_window_only() -> None:
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = trailing_percentile_momentum(series, window=250)
    # Ascending series -- percentile rank should be non-decreasing.
    assert result == sorted(result)
    assert result[0] < result[-1]


def test_trailing_percentile_momentum_never_uses_future_values() -> None:
    # A calm prefix, then one huge spike at the very end. The percentile of
    # every EARLIER position must be identical whether or not that future
    # spike is present in the full series -- proving position i's rank only
    # ever looks backward.
    calm = [1.0, 1.0, 1.0, 1.0, 1.0]
    with_future_spike = calm + [1000.0]
    result_without = trailing_percentile_momentum(calm)
    result_with = trailing_percentile_momentum(with_future_spike)
    assert result_without == result_with[: len(calm)]


def test_trailing_percentile_momentum_handles_none_gracefully() -> None:
    series = [1.0, None, 3.0]
    result = trailing_percentile_momentum(series)
    assert result[1] is None
    assert result[0] is not None and result[2] is not None


# ---- B3: gate direction/threshold filtering ----------------------------------


def test_gate_lt_direction_only_enters_below_threshold() -> None:
    spec = CandidateSpec(
        label="gate_test", risk_mode="gate", risk_gate_direction="lt", risk_gate_threshold=50.0,
        technical_weight=100.0, momentum_weight=0.0, buy_threshold=0.0,  # threshold=0 so composite always clears
    )
    calm = _raw(raw_vol_365=20.0)  # risk_score = 100-20 = 80 (calm, ABOVE the 50 gate)
    volatile = _raw(raw_vol_365=90.0)  # risk_score = 10 (volatile, BELOW the 50 gate)
    _, enter_calm = score_sample(calm, spec)
    _, enter_volatile = score_sample(volatile, spec)
    assert enter_calm is False  # 80 is not < 50
    assert enter_volatile is True  # 10 < 50


def test_gate_gt_direction_is_the_opposite_of_lt() -> None:
    spec_gt = CandidateSpec(
        label="gate_gt", risk_mode="gate", risk_gate_direction="gt", risk_gate_threshold=50.0,
        technical_weight=100.0, momentum_weight=0.0, buy_threshold=0.0,
    )
    calm = _raw(raw_vol_365=20.0)  # risk_score = 80
    _, enter_calm = score_sample(calm, spec_gt)
    assert enter_calm is True  # 80 > 50


def test_gate_never_enters_when_risk_unavailable() -> None:
    spec = CandidateSpec(label="gate_missing", risk_mode="gate", risk_gate_direction="lt", risk_gate_threshold=50.0,
                          technical_weight=100.0, momentum_weight=0.0, buy_threshold=0.0)
    raw = _raw(raw_vol_365=None)
    _, would_enter = score_sample(raw, spec)
    assert would_enter is False  # never fabricated as True on missing data


# ---- Pool boundary partition -------------------------------------------------


def test_train_validation_holdout_pools_never_overlap() -> None:
    assert TRAIN_END < VALIDATION_START
    assert VALIDATION_END < HOLDOUT_START


def test_pool_for_date_assigns_expected_pool() -> None:
    assert pool_for_date("2023-06-15") == "train"
    assert pool_for_date("2024-12-31") == "train"
    assert pool_for_date("2025-03-01") == "validation"
    assert pool_for_date("2025-09-01") == "holdout"
    assert pool_for_date("2026-01-01") == "holdout"
    assert pool_for_date("2020-01-01") is None  # pre-calibration-window history -- excluded, not misclassified


def test_holdout_boundary_is_exclusive_of_validation() -> None:
    assert pool_for_date(VALIDATION_END.isoformat()) == "validation"
    assert pool_for_date(HOLDOUT_START.isoformat()) == "holdout"


# ---- Structural proof: HOLDOUT is never in the default selection pool set ---


def test_evaluate_candidate_default_pools_exclude_holdout() -> None:
    """The single structural guarantee the whole HOLDOUT discipline rests
    on: evaluate_candidate's default `pools` argument must not include
    "holdout" -- every selection/promotion call site in
    entry_calibration_ladder.py uses the default, and only the one
    designated final-evaluation call site in main() passes
    pools=("holdout",) explicitly."""
    import inspect
    sig = inspect.signature(evaluate_candidate)
    default_pools = sig.parameters["pools"].default
    assert "holdout" not in default_pools
    assert default_pools == ("train", "validation")
