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

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", require_text(self.version, "version"))
        for name in ("technical_weight", "momentum_weight", "risk_weight"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, nonnegative=True))
        object.__setattr__(self, "effective_at", require_aware(self.effective_at, "effective_at"))
        total = self.technical_weight + self.momentum_weight + self.risk_weight
        if total <= 0:
            raise ValueError("factor weights must sum to a positive total")
