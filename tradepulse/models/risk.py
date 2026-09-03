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
    # Exit Intelligence (see monitor/coordinator.py::_ratchet_stop/_time_stopped):
    # unrealized-gain % (direction-aware) that first ratchets Holding.current_stop
    # to average_price (break-even). Distinct from the ATR-multiplier below --
    # this is a live, ongoing exit decision, not the entry-time sizing distance
    # atr_stop_multiplier governs.
    break_even_trigger_pct: Decimal = Decimal("4")
    # ATR multiple for the trailing distance BEHIND the position's running high
    # (PositionLot.mfe_price), applied only once break-even has triggered.
    # Deliberately a separate field from atr_stop_multiplier -- "how much room
    # does a new position need" and "how much earned profit will I give back"
    # are independently tunable decisions, not the same number.
    trailing_atr_multiplier: Decimal = Decimal("2.5")
    # Calendar days since the governing (oldest open) lot's opened_at, after
    # which the position is force-closed regardless of stop/target state.
    # 0 disables, mirrors atr_stop_multiplier's existing "0 = off" convention.
    max_hold_days: int = 15
    # Portfolio Optimization (see scanner/coordinator.py's correlation-aware
    # ranking, strategy/correlation.py::pearson_correlation): absolute Pearson
    # correlation of daily returns at/above which a candidate is demoted below
    # non-correlated peers in this cycle's ranking -- never a hard reject.
    max_correlation_threshold: Decimal = Decimal("0.75")
    # Options lane (see scanner/coordinator.py's options branch, risk/engine.py's
    # premium-based stop): stop-loss is a flat pct of entry premium, not
    # ATR-on-underlying; contract selection targets an expiry window and an
    # OTM-pct-of-spot strike; the monitor force-closes within N days of expiry
    # regardless of stop/target state.
    options_premium_stop_pct: Decimal = Decimal("35")
    options_expiry_min_days: int = 21
    options_expiry_max_days: int = 45
    options_target_otm_pct: Decimal = Decimal("3")
    options_forced_close_days_before_expiry: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", require_text(self.profile_id, "profile_id"))
        for name in (
            "max_position_pct", "max_sector_pct", "min_confidence", "stop_loss_pct",
            "max_drawdown_pct", "max_daily_loss_pct", "spread_limit_pct", "slippage_limit_pct",
            "max_risk_per_trade_pct", "max_total_exposure_pct", "min_lot_notional",
            "atr_stop_multiplier", "min_stop_distance_pct", "max_stop_distance_pct",
            "options_premium_stop_pct", "options_target_otm_pct",
            "break_even_trigger_pct", "trailing_atr_multiplier", "max_correlation_threshold",
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
        for name in ("options_expiry_min_days", "options_expiry_max_days", "options_forced_close_days_before_expiry", "max_hold_days"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.options_expiry_min_days > self.options_expiry_max_days:
            raise ValueError("options_expiry_min_days must not exceed options_expiry_max_days")
        if self.max_correlation_threshold > 1:
            raise ValueError("max_correlation_threshold must not exceed 1 (a Pearson correlation coefficient)")
