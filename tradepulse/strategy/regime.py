"""Deterministic market-regime classifier -- direct port of
base44/shared/regime.ts::classifyRegime. Computed from a benchmark (e.g. SPY)
close-price series, NOT LLM estimation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Regime = Literal["low_vol_bull", "high_vol_bear", "range_bound_choppy", "liquidity_crisis", "transition"]

_STRATEGY_NOTES: dict[Regime, str] = {
    "low_vol_bull": "Trend-following - full position sizing, favor momentum breakouts",
    "high_vol_bear": "Defensive / capital preservation - reduce sizing, raise cash, tighten stops",
    "range_bound_choppy": "Mean-reversion - fade extremes, smaller sizes, tight stops",
    "liquidity_crisis": "De-risk / capital preservation - block new buys, exit weak positions",
    "transition": "Adaptive / cautious - moderate sizing, wait for confirmation",
}

# Fraction of the strategy's max position size allowed in each regime.
_POSITION_MULTIPLIERS: dict[Regime, Decimal] = {
    "low_vol_bull": Decimal("1.0"),
    "high_vol_bear": Decimal("0.5"),
    "range_bound_choppy": Decimal("0.7"),
    "liquidity_crisis": Decimal("0.0"),
    "transition": Decimal("0.75"),
}


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    regime: Regime
    confidence: int
    strategy_note: str
    position_multiplier: Decimal
    realized_vol: Decimal | None
    rsi: Decimal | None
    trend: Decimal | None


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _transition() -> RegimeClassification:
    return RegimeClassification(
        regime="transition",
        confidence=30,
        strategy_note=_STRATEGY_NOTES["transition"],
        position_multiplier=_POSITION_MULTIPLIERS["transition"],
        realized_vol=None,
        rsi=None,
        trend=None,
    )


def classify_regime(closes: list[Decimal]) -> RegimeClassification:
    if not closes or len(closes) < 10:
        return _transition()

    values = [float(c) for c in closes]
    series = values[-min(len(values), 288):]
    returns = [
        (series[i] - series[i - 1]) / series[i - 1] for i in range(1, len(series)) if series[i - 1] > 0
    ]
    realized_vol = _std(returns) * math.sqrt(252 * 78)  # annualized, 78 5-min bars/day
    rsi_val = _rsi(series, min(14, len(series) - 1))

    n = len(series)
    mean_y = sum(series) / n
    num = 0.0
    den = 0.0
    for i in range(n):
        x = i - (n - 1) / 2
        num += x * (series[i] - mean_y)
        den += x**2
    slope = num / den if den > 0 else 0.0
    trend = (slope / mean_y) * n if mean_y > 0 else 0.0
    last = series[-1]
    sma_short = sum(series[-12:]) / min(12, len(series))

    high_vol = realized_vol > 0.35
    crisis_vol = realized_vol > 0.80
    bullish = trend > 0.01 and last >= sma_short and rsi_val > 50
    bearish = trend < -0.01 and last <= sma_short and rsi_val < 45

    regime: Regime = "transition"
    if crisis_vol and bearish:
        regime = "liquidity_crisis"
    elif high_vol and bearish:
        regime = "high_vol_bear"
    elif not high_vol and bullish:
        regime = "low_vol_bull"
    elif abs(trend) <= 0.01 and not high_vol:
        regime = "range_bound_choppy"

    agree = 0
    if (trend > 0) == bullish:
        agree += 1
    if (last >= mean_y) == bullish:
        agree += 1
    if (rsi_val > 50) == bullish:
        agree += 1
    if (rsi_val < 50) == bearish:
        agree += 1
    confidence = min(95, max(30, round(40 + (agree / 4) * 50 + abs(trend) * 200)))

    return RegimeClassification(
        regime=regime,
        confidence=confidence,
        strategy_note=_STRATEGY_NOTES[regime],
        position_multiplier=_POSITION_MULTIPLIERS[regime],
        realized_vol=Decimal(str(round(realized_vol, 3))),
        rsi=Decimal(str(round(rsi_val, 1))),
        trend=Decimal(str(round(trend, 4))),
    )
