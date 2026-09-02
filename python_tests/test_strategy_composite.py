from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.models import StrategyWeights
from tradepulse.strategy.composite import factor_breakdown, signal_from_composite, weighted_composite
from tradepulse.strategy.factors import FactorScores


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _weights() -> StrategyWeights:
    return StrategyWeights("v1", Decimal("0.40"), Decimal("0.35"), Decimal("0.25"), NOW)


def test_weighted_composite_of_uniform_scores_equals_that_score() -> None:
    scores = FactorScores(Decimal("100"), Decimal("70"), Decimal("70"), Decimal("70"), None, None, None)
    assert weighted_composite(scores, _weights()) == Decimal("70")


def test_weighted_composite_respects_relative_weights() -> None:
    scores = FactorScores(Decimal("100"), Decimal("100"), Decimal("0"), Decimal("0"), None, None, None)
    composite = weighted_composite(scores, _weights())
    assert composite == Decimal("40")  # only technical_weight (0.40) contributes


def test_signal_thresholds() -> None:
    assert signal_from_composite(Decimal("85")) == "STRONG_BUY"
    assert signal_from_composite(Decimal("70")) == "BUY"
    assert signal_from_composite(Decimal("50")) == "HOLD"
    assert signal_from_composite(Decimal("35")) == "SELL"
    assert signal_from_composite(Decimal("10")) == "STRONG_SELL"


def _six_factor_weights() -> StrategyWeights:
    return StrategyWeights(
        "v1", Decimal("20"), Decimal("15"), Decimal("15"), NOW,
        liquidity_weight=Decimal("15"), risk_quality_weight=Decimal("15"), relative_strength_weight=Decimal("20"),
    )


def test_weighted_composite_skips_none_relative_strength_score_from_both_numerator_and_denominator() -> None:
    # relative_strength_score is None (no benchmark this cycle) even though
    # its weight (20) is nonzero -- must be excluded from BOTH the
    # numerator and denominator, not treated as a zero score that drags the
    # average down.
    scores = FactorScores(Decimal("100"), Decimal("80"), Decimal("80"), Decimal("80"), None, None, None, Decimal("80"), Decimal("80"), None)
    weights = _six_factor_weights()
    composite = weighted_composite(scores, weights)
    assert composite == Decimal("80")  # every present factor scored 80 -> renormalized average is still 80


def test_weighted_composite_skips_zero_weighted_factors() -> None:
    # Pins the backward-compatibility guarantee: with only technical/
    # momentum/risk weighted (every pre-Phase-1 caller), the new fields'
    # default scores (liquidity=50, risk_quality=50) must be fully excluded
    # even though they're real (non-None) values.
    scores = FactorScores(Decimal("100"), Decimal("70"), Decimal("70"), Decimal("70"), None, None, None)
    assert weighted_composite(scores, _weights()) == Decimal("70")


def test_weighted_composite_over_six_factors_matches_hand_computed_average() -> None:
    scores = FactorScores(
        Decimal("100"), Decimal("90"), Decimal("60"), Decimal("40"), None, None, None,
        liquidity_score=Decimal("70"), risk_quality_score=Decimal("30"), relative_strength_score=Decimal("55"),
    )
    weights = _six_factor_weights()
    composite = weighted_composite(scores, weights)
    numerator = 90 * 20 + 60 * 15 + 40 * 15 + 70 * 15 + 30 * 15 + 55 * 20
    denominator = 20 + 15 + 15 + 15 + 15 + 20
    assert composite == Decimal(numerator) / Decimal(denominator)


def test_factor_breakdown_labels_match_user_facing_names() -> None:
    scores = FactorScores(
        Decimal("100"), Decimal("82"), Decimal("75"), Decimal("79"), None, None, None,
        liquidity_score=Decimal("91"), risk_quality_score=Decimal("79"), relative_strength_score=Decimal("88"),
    )
    assert factor_breakdown(scores) == {
        "trend": "82", "momentum": "75", "liquidity": "91", "risk_quality": "79", "relative_strength": "88",
    }


def test_factor_breakdown_reports_unavailable_for_missing_relative_strength() -> None:
    scores = FactorScores(Decimal("100"), Decimal("70"), Decimal("70"), Decimal("70"), None, None, None)
    assert factor_breakdown(scores)["relative_strength"] == "unavailable"
