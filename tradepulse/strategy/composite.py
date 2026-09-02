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

# Strategy Sophistication Phase 1 -- generalized from the original 3-term
# (technical/momentum/risk) formula to N factor/weight pairs. Each pair
# lists a FactorScores attribute alongside the StrategyWeights attribute
# that weights it.
_FACTOR_PAIRS: tuple[tuple[str, str], ...] = (
    ("technical_score", "technical_weight"),
    ("momentum_score", "momentum_weight"),
    ("risk_score", "risk_weight"),
    ("liquidity_score", "liquidity_weight"),
    ("risk_quality_score", "risk_quality_weight"),
    ("relative_strength_score", "relative_strength_weight"),
)


def weighted_composite(scores: FactorScores, weights: StrategyWeights) -> Decimal:
    numerator = Decimal("0")
    denominator = Decimal("0")
    for score_attr, weight_attr in _FACTOR_PAIRS:
        score = getattr(scores, score_attr)
        weight = getattr(weights, weight_attr)
        if score is None or weight <= 0:
            # A None score (relative_strength_score with no benchmark this
            # cycle) or a zero weight (this regime's profile doesn't use
            # this factor) both exclude the factor from numerator AND
            # denominator -- a renormalized average over what's actually
            # available/weighted this cycle, never a penalty for missing
            # data. With only technical/momentum/risk ever nonzero (every
            # pre-Phase-1 caller), this reduces to exactly the original
            # 3-term formula.
            continue
        numerator += score * weight
        denominator += weight
    if denominator <= 0:
        denominator = Decimal("1")
    return numerator / denominator


def factor_breakdown(scores: FactorScores) -> dict[str, str]:
    """Transparent, human/dashboard-facing labeling of a FactorScores --
    single source of truth for what each field is called outside this
    package. relative_strength_score is labeled honestly as its own
    concept (candidate momentum vs. benchmark momentum), not disguised as
    "regime fit" -- regime-conditioned weighting (config/strategy_weights.py
    ::regime_conditioned_weights) is what actually produces "fit": a
    candidate whose strong factors are the ones the current regime weights
    heavily will naturally score higher, with no separate synthetic number
    needed."""
    return {
        "trend": str(scores.technical_score),
        "momentum": str(scores.momentum_score),
        "liquidity": str(scores.liquidity_score),
        "risk_quality": str(scores.risk_quality_score),
        "relative_strength": (
            str(scores.relative_strength_score) if scores.relative_strength_score is not None else "unavailable"
        ),
    }


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
