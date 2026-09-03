from tradepulse.strategy.correlation import pearson_correlation


def test_pearson_correlation_is_1_for_perfectly_proportional_series() -> None:
    a = [100.0, 101.0, 102.0, 101.0, 103.0]
    b = [50.0, 50.5, 51.0, 50.5, 51.5]  # exactly half of a's returns, same sign every step
    result = pearson_correlation(a, b)
    assert result is not None
    assert abs(result - 1.0) < 1e-9


def test_pearson_correlation_is_negative_1_for_perfectly_inverse_series() -> None:
    a = [100.0]
    b = [50.0]
    for pct in (0.01, 0.02, -0.01, 0.03):  # exact returns, so b's exact-inverse returns are guaranteed exact too
        a.append(a[-1] * (1 + pct))
        b.append(b[-1] * (1 - pct))
    result = pearson_correlation(a, b)
    assert result is not None
    assert abs(result - (-1.0)) < 1e-9


def test_pearson_correlation_is_none_with_fewer_than_two_returns() -> None:
    assert pearson_correlation([100.0], [50.0]) is None
    assert pearson_correlation([100.0, 101.0], [50.0]) is None


def test_pearson_correlation_is_none_for_a_flat_zero_variance_series() -> None:
    a = [100.0, 101.0, 102.0, 103.0]
    flat = [50.0, 50.0, 50.0, 50.0]
    assert pearson_correlation(a, flat) is None


def test_pearson_correlation_uses_returns_not_raw_prices() -> None:
    """Two series with wildly different price levels but IDENTICAL daily
    percentage moves must still correlate perfectly -- proves this isn't
    accidentally comparing raw prices (which would understate/distort the
    relationship at different price scales)."""
    a = [10.0, 11.0, 12.1, 11.0]  # +10%, +10%, -9.09...%
    b = [1000.0, 1100.0, 1210.0, 1100.0]  # identical pct moves, 100x the scale
    result = pearson_correlation(a, b)
    assert result is not None
    assert abs(result - 1.0) < 1e-9
