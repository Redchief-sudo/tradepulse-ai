"""Weighted composite score and signal bucketing -- port of
base44/shared/quantScore.ts::weightedComposite/signalFromComposite, restricted
to the technical/momentum/risk factors this MVP actually computes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from tradepulse.models import StrategyWeights

from .factors import FactorScores

Signal = Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]


def weighted_composite(scores: FactorScores, weights: StrategyWeights) -> Decimal:
    total = weights.technical_weight + weights.momentum_weight + weights.risk_weight
    if total <= 0:
        total = Decimal("1")
    return (
        scores.technical_score * weights.technical_weight
        + scores.momentum_score * weights.momentum_weight
        + scores.risk_score * weights.risk_weight
    ) / total


def signal_from_composite(composite: Decimal) -> Signal:
    if composite > 80:
        return "STRONG_BUY"
    if composite > 65:
        return "BUY"
    if composite > 45:
        return "HOLD"
    if composite > 30:
        return "SELL"
    return "STRONG_SELL"
