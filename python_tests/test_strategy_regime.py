from decimal import Decimal

from tradepulse.strategy.regime import classify_regime


def test_insufficient_history_returns_transition_with_no_stats() -> None:
    result = classify_regime([Decimal("100")] * 5)
    assert result.regime == "transition"
    assert result.confidence == 30
    assert result.realized_vol is None
    assert result.rsi is None
    assert result.trend is None


def test_flat_series_is_range_bound_choppy() -> None:
    closes = [Decimal("100")] * 30
    result = classify_regime(closes)
    assert result.regime == "range_bound_choppy"
    assert result.position_multiplier == Decimal("0.7")


def test_steady_uptrend_is_low_vol_bull() -> None:
    closes = [Decimal(100 + i * 0.05) for i in range(60)]
    result = classify_regime(closes)
    assert result.regime == "low_vol_bull"
    assert result.position_multiplier == Decimal("1.0")


def test_confidence_is_bounded_between_30_and_95() -> None:
    closes = [Decimal(100 + i * 0.05) for i in range(60)]
    result = classify_regime(closes)
    assert 30 <= result.confidence <= 95
