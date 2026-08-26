from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .base import decimal_value, require_text


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """A named risk profile's numeric limits. Not persisted per-record — loaded
    from config/risk_profiles.py and stamped into TradeIntent.risk_snapshot and
    ScanRun details for audit traceability of which limits produced a decision.
    """

    profile_id: str
    max_position_pct: Decimal
    max_sector_pct: Decimal
    min_confidence: Decimal
    max_daily_trades: int
    stop_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_open_positions: int
    max_daily_loss_pct: Decimal
    spread_limit_pct: Decimal
    slippage_limit_pct: Decimal
    max_risk_per_trade_pct: Decimal
    max_total_exposure_pct: Decimal
    max_simultaneous_orders: int
    min_lot_notional: Decimal = Decimal("1")
    # Dynamic position sizing (see risk/engine.py::evaluate_risk):
    min_position_size_multiplier: Decimal = Decimal("0.5")  # risk-budget multiplier at exactly min_confidence; 1.0 at confidence=100
    atr_stop_multiplier: Decimal = Decimal("2")  # ATR-based stop distance = atr_stop_multiplier * ATR, replacing the fixed stop_loss_pct
    min_stop_distance_pct: Decimal = Decimal("0.5")  # ATR stop sanity band -- outside [min, max] falls back to stop_loss_pct
    max_stop_distance_pct: Decimal = Decimal("25")

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", require_text(self.profile_id, "profile_id"))
        for name in (
            "max_position_pct", "max_sector_pct", "min_confidence", "stop_loss_pct",
            "max_drawdown_pct", "max_daily_loss_pct", "spread_limit_pct", "slippage_limit_pct",
            "max_risk_per_trade_pct", "max_total_exposure_pct", "min_lot_notional",
            "atr_stop_multiplier", "min_stop_distance_pct", "max_stop_distance_pct",
        ):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, nonnegative=True))
        for name in ("max_daily_trades", "max_open_positions", "max_simultaneous_orders"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if not (0 <= self.min_confidence <= 100):
            raise ValueError("min_confidence must be between 0 and 100")
        object.__setattr__(self, "min_position_size_multiplier", decimal_value(self.min_position_size_multiplier, "min_position_size_multiplier"))
        if not (0 < self.min_position_size_multiplier <= 1):
            raise ValueError("min_position_size_multiplier must be between 0 (exclusive) and 1 (inclusive)")
