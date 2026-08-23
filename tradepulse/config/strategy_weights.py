"""Default factor-weight vector for the scanner's deterministic composite
gate -- see strategy/composite.py.

These are the *relative* weights from the audited base44 source
(quantScore.ts's default `{technical: 25, fundamental: 25, sentiment: 20,
momentum: 15, risk: 15}`), with the fundamental/sentiment factors this MVP
never computes dropped. weighted_composite() already divides by the sum of
the weights actually passed in, so keeping the same relative proportions
among technical/momentum/risk is a faithful port, not an invented number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tradepulse.models import StrategyWeights


def default_strategy_weights(now: datetime) -> StrategyWeights:
    return StrategyWeights(
        version="v1", technical_weight=Decimal("25"), momentum_weight=Decimal("15"), risk_weight=Decimal("15"),
        effective_at=now,
    )
