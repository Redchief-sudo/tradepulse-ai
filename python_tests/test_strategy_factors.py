from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.models import Candle
from tradepulse.strategy.factors import compute_real_factors


def _candles(closes: list[float], volumes: list[float] | None = None) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    volumes = volumes if volumes is not None else [1000.0] * len(closes)
    return [
        Candle(
            date=(start + timedelta(days=i)).date().isoformat(),
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=Decimal(str(v)),
        )
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_returns_none_with_fewer_than_30_candles() -> None:
    assert compute_real_factors(_candles([100.0] * 29)) is None


def test_strong_uptrend_produces_high_momentum_and_low_risk_scores() -> None:
    # technical_score is intentionally RSI-mean-reversion-flavored: a
    # monotonic uptrend pegs RSI at 100 (all gains, zero losses), and
    # `technical += (50 - rsi) * 0.5` then PENALIZES technical_score at that
    # extreme -- this is a faithful port of quantScore.ts's formula, not a
    # bug, so this test does not assert a direction on technical_score.
    closes = [100.0 + i for i in range(60)]  # steady uptrend
    scores = compute_real_factors(_candles(closes))
    assert scores is not None
    assert scores.momentum_score > Decimal("50")
    assert scores.risk_score > Decimal("90")  # near-zero realized volatility
    assert scores.price == Decimal(str(closes[-1]))


def test_flat_series_produces_neutral_scores() -> None:
    closes = [100.0] * 60
    scores = compute_real_factors(_candles(closes))
    assert scores is not None
    assert scores.momentum_score == Decimal("50")


def test_scores_are_clamped_to_0_100_range() -> None:
    closes = [100.0 + i * 5 for i in range(60)]  # aggressive uptrend
    scores = compute_real_factors(_candles(closes))
    assert scores is not None
    assert Decimal("0") <= scores.technical_score <= Decimal("100")
    assert Decimal("0") <= scores.momentum_score <= Decimal("100")
    assert Decimal("0") <= scores.risk_score <= Decimal("100")
    assert Decimal("0") <= scores.liquidity_score <= Decimal("100")
    assert Decimal("0") <= scores.risk_quality_score <= Decimal("100")
    assert scores.relative_strength_score is None  # no benchmark_closes supplied


def test_compute_real_factors_defaults_calendar_to_equity_for_backward_compatible_callers() -> None:
    # Every pre-Phase-1 call site invokes compute_real_factors(candles) with
    # no keyword args -- must still work and produce a real risk_quality_score.
    closes = [100.0 + i for i in range(60)]
    scores = compute_real_factors(_candles(closes))
    assert scores is not None
    assert scores.risk_quality_score != Decimal("50")  # a real ATR-based value was computed, not the no-data default


def test_liquidity_score_rewards_volume_confirming_uptrend_over_thin_volume_rally() -> None:
    closes = [100.0 + i for i in range(60)]  # steady uptrend, every bar an up day
    confirming_volumes = [500.0 + i * 20 for i in range(60)]  # rising volume, elevated at the end
    thin_volumes = [1000.0] * 55 + [100.0] * 5  # volume dries up right as price keeps rising
    confirming = compute_real_factors(_candles(closes, confirming_volumes))
    thin = compute_real_factors(_candles(closes, thin_volumes))
    assert confirming is not None and thin is not None
    assert confirming.liquidity_score > thin.liquidity_score


def test_risk_quality_score_is_high_for_tight_range_low_for_choppy_range() -> None:
    tight_closes = [100.0 + i * 0.05 for i in range(60)]
    # A wide, gap-heavy series -- deliberately large day-to-day swings so
    # ATR% clears the equity "wide/choppy" threshold.
    choppy_closes = [100.0]
    for i in range(59):
        choppy_closes.append(choppy_closes[-1] + (15.0 if i % 2 == 0 else -14.0))
    tight = compute_real_factors(_candles(tight_closes))
    choppy = compute_real_factors(_candles(choppy_closes))
    assert tight is not None and choppy is not None
    assert tight.risk_quality_score > Decimal("70")
    assert choppy.risk_quality_score < Decimal("30")
    assert tight.risk_quality_score > choppy.risk_quality_score


def test_relative_strength_score_is_none_without_benchmark_closes() -> None:
    closes = [100.0 + i for i in range(60)]
    scores = compute_real_factors(_candles(closes))
    assert scores is not None
    assert scores.relative_strength_score is None


def test_relative_strength_score_positive_when_outperforming_benchmark() -> None:
    closes = [100.0 + i for i in range(60)]  # +1/day
    benchmark = [Decimal(str(100.0 + i * 0.2)) for i in range(60)]  # milder uptrend
    scores = compute_real_factors(_candles(closes), benchmark_closes=benchmark)
    assert scores is not None
    assert scores.relative_strength_score is not None
    assert scores.relative_strength_score > Decimal("50")


def test_relative_strength_score_negative_when_underperforming_benchmark() -> None:
    closes = [100.0 + i for i in range(60)]  # +1/day
    benchmark = [Decimal(str(100.0 + i * 3)) for i in range(60)]  # benchmark outpaces the candidate
    scores = compute_real_factors(_candles(closes), benchmark_closes=benchmark)
    assert scores is not None
    assert scores.relative_strength_score is not None
    assert scores.relative_strength_score < Decimal("50")
