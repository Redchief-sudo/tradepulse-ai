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

MIN_CANDLES = 30


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


def compute_real_factors(candles: list[Candle]) -> FactorScores | None:
    if not candles or len(candles) < MIN_CANDLES:
        return None

    closes = [float(c.close) for c in candles]

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

    return FactorScores(
        price=candles[-1].close,
        technical_score=_dec(technical),
        momentum_score=_dec(momentum_score),
        risk_score=_dec(risk_score),
        rsi=_dec(rsi_val),
        ma50=_dec(ma50),
        ma200=_dec(ma200),
    )
