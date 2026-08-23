from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.models import Candle
from tradepulse.strategy.factors import compute_real_factors


def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            date=(start + timedelta(days=i)).date().isoformat(),
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=Decimal("1000"),
        )
        for i, c in enumerate(closes)
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
