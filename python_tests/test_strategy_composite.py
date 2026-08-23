from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.models import StrategyWeights
from tradepulse.strategy.composite import signal_from_composite, weighted_composite
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
