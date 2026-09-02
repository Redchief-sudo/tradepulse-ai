"""Deterministic quant factor scores from OHLCV -- port of
base44/shared/quantScore.ts::computeRealFactors.

The governed production model uses only these three deterministic factors.
Fundamental/sentiment factors are explicitly out of MVP scope (Base44's own
version also permanently zero-weights them) -- do not fabricate placeholder
versions of them here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradepulse.models import Candle

from . import indicators as ind
from .regime import Calendar

MIN_CANDLES = 30

# ATR%-quality thresholds -- PLACEHOLDER, NOT YET CALIBRATED against real
# data. Illustrative only, loosely extrapolated from regime.py's own
# equity->crypto volatility-threshold ratio. Needs a real calibration pass
# (same discipline as docs/regime-classifier-phase1-calibration.md) once
# there is outcome-attribution data to calibrate against -- shipping a
# clearly-labeled guess now beats blocking this phase on calibrating
# against nothing.
@dataclass(frozen=True, slots=True)
class _AtrQualityThresholds:
    tight_pct: Decimal  # ATR% at/below which range is "tight/controlled" -> quality 100
    wide_pct: Decimal  # ATR% at/above which range is "choppy/gappy" -> quality 0


_ATR_QUALITY_THRESHOLDS: dict[Calendar, _AtrQualityThresholds] = {
    "equity": _AtrQualityThresholds(tight_pct=Decimal("1.0"), wide_pct=Decimal("5.0")),
    "crypto": _AtrQualityThresholds(tight_pct=Decimal("2.5"), wide_pct=Decimal("8.0")),
}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 6)))


@dataclass(frozen=True, slots=True)
class FactorScores:
    price: Decimal
    technical_score: Decimal
    momentum_score: Decimal
    risk_score: Decimal
    rsi: Decimal | None
    ma50: Decimal | None
    ma200: Decimal | None
    # Strategy Sophistication Phase 1 additions -- all additive, all default
    # so every pre-existing direct FactorScores(...) construction (7
    # positional args) in tests stays valid unchanged.
    #
    # liquidity_score / risk_quality_score are always computed by
    # compute_real_factors once MIN_CANDLES is met (Candle.volume/high/low
    # are always present) -- the Decimal("50") default only matters for
    # direct construction bypassing compute_real_factors (e.g. tests).
    liquidity_score: Decimal = Decimal("50")
    # risk_quality_score (ATR%-based range quality) is deliberately DISTINCT
    # from risk_score (100 - annualized stdev of close-to-close returns)
    # despite both being volatility-flavored: stdev-of-closes misses the
    # intraday/gap range that ATR (high-low-close true range) captures, so
    # the two are correlated but not redundant.
    risk_quality_score: Decimal = Decimal("50")
    # Genuinely nullable in production -- None whenever no benchmark_closes
    # was supplied to compute_real_factors this cycle (e.g. the lane's
    # regime benchmark fetch failed).
    relative_strength_score: Decimal | None = None


def compute_real_factors(
    candles: list[Candle], *, calendar: Calendar = "equity", benchmark_closes: list[Decimal] | None = None,
) -> FactorScores | None:
    if not candles or len(candles) < MIN_CANDLES:
        return None

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    volumes = [float(c.volume) for c in candles]

    rsi_val = ind.rsi(closes, 14)
    macd_val = ind.macd(closes)
    boll = ind.bollinger(closes, 20, 2)
    ma50 = ind.sma(closes, 50)
    ma200 = ind.sma(closes, 200)
    mom = ind.momentum(closes, 14)
    vol = ind.volatility(closes, 20)

    technical = 50.0
    if rsi_val is not None:
        technical += (50 - rsi_val) * 0.5
    if macd_val is not None:
        technical += 10 if macd_val.histogram > 0 else -10
    if ma50 is not None and ma200 is not None:
        technical += 10 if ma50 > ma200 else -10
    if boll is not None:
        if boll.percent_b < 20:
            technical += 8
        elif boll.percent_b > 80:
            technical -= 8
    technical = _clamp(technical)

    momentum_score = _clamp(50 + (mom or 0.0) * 2)
    risk_score = _clamp(100 - (vol if vol is not None else 50.0))

    # Liquidity/volume-confirmation factor -- baseline 50, bonuses/penalties,
    # same style as the technical formula above.
    obv_series = ind.obv(closes, volumes)
    obv_trend = ind.momentum(obv_series, 14) if obv_series is not None else None
    vol_avg20 = ind.sma(volumes, 20)
    liquidity = 50.0
    if obv_trend is not None and mom is not None:
        liquidity += 15 if (obv_trend > 0) == (mom > 0) else -15
    if vol_avg20 is not None and vol_avg20 > 0:
        ratio = volumes[-1] / vol_avg20
        liquidity += 10 if ratio > 1.5 else (-10 if ratio < 0.5 else 0)
    liquidity_score = _clamp(liquidity)

    # Risk-quality factor -- ATR%, linearly interpolated between calibrated
    # tight/wide bands (see _ATR_QUALITY_THRESHOLDS).
    atr_val = ind.atr(highs, lows, closes, 14)
    atr_pct = (atr_val / closes[-1]) * 100 if atr_val is not None and closes[-1] > 0 else None
    if atr_pct is None:
        risk_quality_score = 50.0
    else:
        thresholds = _ATR_QUALITY_THRESHOLDS[calendar]
        span = float(thresholds.wide_pct - thresholds.tight_pct)
        risk_quality_score = (
            _clamp(100 - ((atr_pct - float(thresholds.tight_pct)) / span) * 100) if span > 0 else 50.0
        )

    # Relative-strength-vs-benchmark factor -- candidate momentum minus
    # benchmark momentum, reusing momentum_score's own "*2" scaling constant
    # applied to an excess-return quantity instead of a raw one. None
    # whenever no benchmark series was supplied.
    relative_strength_score = None
    if benchmark_closes is not None:
        bench_mom = ind.momentum([float(c) for c in benchmark_closes], 14)
        if mom is not None and bench_mom is not None:
            relative_strength_score = _dec(_clamp(50 + (mom - bench_mom) * 2))

    return FactorScores(
        price=candles[-1].close,
        technical_score=_dec(technical),
        momentum_score=_dec(momentum_score),
        risk_score=_dec(risk_score),
        rsi=_dec(rsi_val),
        ma50=_dec(ma50),
        ma200=_dec(ma200),
        liquidity_score=_dec(liquidity_score),
        risk_quality_score=_dec(risk_quality_score),
        relative_strength_score=relative_strength_score,
    )
