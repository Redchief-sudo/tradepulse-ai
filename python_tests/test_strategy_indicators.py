from tradepulse.strategy.indicators import bollinger, macd, momentum, rsi, sma, volatility


def test_sma_averages_the_trailing_window() -> None:
    assert sma([1, 2, 3, 4, 5], 5) == 3
    assert sma([1, 2, 3, 4, 5], 3) == 4
    assert sma([1, 2], 5) is None


def test_rsi_is_100_when_all_recent_moves_are_gains() -> None:
    closes = [float(i) for i in range(1, 16)]  # 1..15, strictly increasing
    assert rsi(closes, 14) == 100.0


def test_rsi_returns_none_with_insufficient_history() -> None:
    assert rsi([1.0, 2.0, 3.0], 14) is None


def test_momentum_computes_percent_change_over_period() -> None:
    closes = [100.0] * 10 + [110.0]
    assert momentum(closes, 10) == 10.0


def test_volatility_is_zero_for_a_flat_series() -> None:
    closes = [100.0] * 25
    assert volatility(closes, 20) == 0.0


def test_bollinger_percent_b_is_50_for_zero_width_band() -> None:
    closes = [100.0] * 20
    result = bollinger(closes, 20, 2)
    assert result is not None
    assert result.percent_b == 50.0
    assert result.middle == 100.0


def test_macd_requires_enough_history() -> None:
    assert macd([1.0] * 10) is None
