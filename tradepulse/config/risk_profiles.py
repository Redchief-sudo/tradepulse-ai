"""Risk-profile presets.

Numeric values are a direct port of base44/shared/riskEngine.ts's RISK_LIMITS
table (verified against source, not approximated) — these are the audited,
already-tuned starting defaults referenced in the migration plan. Adaptive
tuning is out of MVP scope; these are manually revisited config.
"""

from __future__ import annotations

from decimal import Decimal

from tradepulse.models import RiskLimits

RISK_PROFILES: dict[str, RiskLimits] = {
    "aggressive": RiskLimits(
        profile_id="aggressive",
        max_position_pct=Decimal("15"), max_sector_pct=Decimal("40"), min_confidence=Decimal("70"),
        max_daily_trades=8, stop_loss_pct=Decimal("12"), max_drawdown_pct=Decimal("25"),
        max_open_positions=12, max_daily_loss_pct=Decimal("5"),
        spread_limit_pct=Decimal("2"), slippage_limit_pct=Decimal("1.5"),
        max_risk_per_trade_pct=Decimal("0.50"), max_total_exposure_pct=Decimal("60"),
        max_simultaneous_orders=5,
    ),
    "balanced": RiskLimits(
        profile_id="balanced",
        max_position_pct=Decimal("7"), max_sector_pct=Decimal("20"), min_confidence=Decimal("80"),
        max_daily_trades=3, stop_loss_pct=Decimal("8"), max_drawdown_pct=Decimal("15"),
        max_open_positions=5, max_daily_loss_pct=Decimal("1.0"),
        spread_limit_pct=Decimal("1.5"), slippage_limit_pct=Decimal("1"),
        max_risk_per_trade_pct=Decimal("0.30"), max_total_exposure_pct=Decimal("40"),
        max_simultaneous_orders=2,
    ),
    "conservative": RiskLimits(
        profile_id="conservative",
        max_position_pct=Decimal("5"), max_sector_pct=Decimal("15"), min_confidence=Decimal("88"),
        max_daily_trades=2, stop_loss_pct=Decimal("5"), max_drawdown_pct=Decimal("8"),
        max_open_positions=3, max_daily_loss_pct=Decimal("0.5"),
        spread_limit_pct=Decimal("1"), slippage_limit_pct=Decimal("0.5"),
        max_risk_per_trade_pct=Decimal("0.25"), max_total_exposure_pct=Decimal("30"),
        max_simultaneous_orders=2,
    ),
    "micro": RiskLimits(
        profile_id="micro",
        max_position_pct=Decimal("20"), max_sector_pct=Decimal("50"), min_confidence=Decimal("82"),
        max_daily_trades=2, stop_loss_pct=Decimal("6"), max_drawdown_pct=Decimal("10"),
        max_open_positions=3, max_daily_loss_pct=Decimal("2"),
        spread_limit_pct=Decimal("2"), slippage_limit_pct=Decimal("1"),
        max_risk_per_trade_pct=Decimal("1.0"), max_total_exposure_pct=Decimal("70"),
        max_simultaneous_orders=2,
    ),
}


def risk_limits_for_profile(profile_id: str) -> RiskLimits:
    return RISK_PROFILES.get(profile_id, RISK_PROFILES["balanced"])
