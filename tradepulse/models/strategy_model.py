from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from .base import decimal_value, require_aware, require_text

WeightSource = Literal["config"]


@dataclass(frozen=True, slots=True)
class StrategyWeights:
    """A fixed, config-loaded factor-weight vector. Adaptive/statistically-
    gated weight promotion (the Base44 modelGovernance.ts equivalent) is
    explicitly deferred post-MVP — this is manually revisited config, not a
    learned or persisted-per-version record.
    """

    version: str
    technical_weight: Decimal
    momentum_weight: Decimal
    risk_weight: Decimal
    effective_at: datetime
    source: WeightSource = "config"
    # Strategy Sophistication Phase 1 -- additive, all default to 0 so every
    # pre-existing caller/test that only ever set technical/momentum/risk
    # weights is numerically unaffected (a zero weight excludes that factor
    # from the composite entirely -- see strategy/composite.py::weighted_composite).
    liquidity_weight: Decimal = Decimal("0")
    risk_quality_weight: Decimal = Decimal("0")
    relative_strength_weight: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", require_text(self.version, "version"))
        for name in (
            "technical_weight", "momentum_weight", "risk_weight",
            "liquidity_weight", "risk_quality_weight", "relative_strength_weight",
        ):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, nonnegative=True))
        object.__setattr__(self, "effective_at", require_aware(self.effective_at, "effective_at"))
        total = (
            self.technical_weight + self.momentum_weight + self.risk_weight
            + self.liquidity_weight + self.risk_quality_weight + self.relative_strength_weight
        )
        if total <= 0:
            raise ValueError("factor weights must sum to a positive total")
