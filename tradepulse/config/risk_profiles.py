"""Risk-profile presets.

Numeric values are a direct port of base44/shared/riskEngine.ts's RISK_LIMITS
table (verified against source, not approximated) — these are the audited,
already-tuned starting defaults referenced in the migration plan. Adaptive
tuning is out of MVP scope; these are manually revisited config.

Exit Intelligence (break_even_trigger_pct/trailing_atr_multiplier/max_hold_days)
and Portfolio Optimization (max_correlation_threshold) fields below are NOT
part of that ported table -- new to this system. See
docs/exit-intelligence-portfolio-optimization-calibration.md for the full
empirical grounding pass (real historical Alpaca data, not simulated P&L):
max_correlation_threshold below is set from real pairwise correlation
percentiles across the default universe; trailing_atr_multiplier was
validated against real ATR% distributions and kept unchanged;
break_even_trigger_pct and max_hold_days remain reasoned judgment calls (no
clean market-statistic answers "how much gain to lock in" or "how long is
too long without a real backtest/paper-trade dataset) -- flagged for
revisiting once Outcome Attribution has real trade outcomes.

max_correlation_threshold_crypto (Rev.81 corrective round) is anchored to
crypto's own real pairwise |correlation| distribution (10 pairs, 5 symbols
-- small sample, explicitly weaker confidence than the equity thresholds
above), not equity's percentile posture reused verbatim: crypto's median is
~0.67, well above every equity-derived threshold except aggressive's, so a
single global threshold was flagging ordinary crypto co-movement (e.g.
BTC/ETH) as concentrated by default. See
docs/exit-intelligence-portfolio-optimization-calibration.md's Finding 2
addendum.
"""

from __future__ import annotations

from decimal import Decimal

from tradepulse.models import RiskLimits

from .settings import SettingsError

RISK_PROFILES: dict[str, RiskLimits] = {
    "aggressive": RiskLimits(
        profile_id="aggressive",
        max_position_pct=Decimal("15"), max_sector_pct=Decimal("40"), min_confidence=Decimal("70"),
        max_daily_trades=8, stop_loss_pct=Decimal("12"), max_drawdown_pct=Decimal("25"),
        max_open_positions=12, max_daily_loss_pct=Decimal("5"),
        spread_limit_pct=Decimal("2"), slippage_limit_pct=Decimal("1.5"),
        max_risk_per_trade_pct=Decimal("0.50"), max_total_exposure_pct=Decimal("60"),
        max_simultaneous_orders=5, min_position_size_multiplier=Decimal("0.65"),
        options_premium_stop_pct=Decimal("45"), options_expiry_min_days=14, options_expiry_max_days=30,
        options_target_otm_pct=Decimal("5"), options_forced_close_days_before_expiry=2,
        break_even_trigger_pct=Decimal("6"), trailing_atr_multiplier=Decimal("3.0"), max_hold_days=10,
        # ~p95 of real equity pairwise |correlation| across the default
        # universe -- see docs/exit-intelligence-portfolio-optimization-calibration.md
        max_correlation_threshold=Decimal("0.75"),
        max_correlation_threshold_crypto=Decimal("0.85"),
    ),
    "balanced": RiskLimits(
        profile_id="balanced",
        max_position_pct=Decimal("7"), max_sector_pct=Decimal("20"), min_confidence=Decimal("80"),
        max_daily_trades=3, stop_loss_pct=Decimal("8"), max_drawdown_pct=Decimal("15"),
        max_open_positions=5, max_daily_loss_pct=Decimal("1.0"),
        spread_limit_pct=Decimal("1.5"), slippage_limit_pct=Decimal("1"),
        max_risk_per_trade_pct=Decimal("0.30"), max_total_exposure_pct=Decimal("40"),
        max_simultaneous_orders=2, min_position_size_multiplier=Decimal("0.5"),
        options_premium_stop_pct=Decimal("35"), options_expiry_min_days=21, options_expiry_max_days=45,
        options_target_otm_pct=Decimal("3"), options_forced_close_days_before_expiry=2,
        break_even_trigger_pct=Decimal("4"), trailing_atr_multiplier=Decimal("2.5"), max_hold_days=15,
        # ~p90 -- catches JPM/BAC-level (0.82) sector concentration
        max_correlation_threshold=Decimal("0.65"),
        max_correlation_threshold_crypto=Decimal("0.80"),
    ),
    "conservative": RiskLimits(
        profile_id="conservative",
        max_position_pct=Decimal("5"), max_sector_pct=Decimal("15"), min_confidence=Decimal("88"),
        max_daily_trades=2, stop_loss_pct=Decimal("5"), max_drawdown_pct=Decimal("8"),
        max_open_positions=3, max_daily_loss_pct=Decimal("0.5"),
        spread_limit_pct=Decimal("1"), slippage_limit_pct=Decimal("0.5"),
        max_risk_per_trade_pct=Decimal("0.25"), max_total_exposure_pct=Decimal("30"),
        max_simultaneous_orders=2, min_position_size_multiplier=Decimal("0.35"),
        options_premium_stop_pct=Decimal("25"), options_expiry_min_days=30, options_expiry_max_days=60,
        options_target_otm_pct=Decimal("1.5"), options_forced_close_days_before_expiry=3,
        break_even_trigger_pct=Decimal("2.5"), trailing_atr_multiplier=Decimal("2.0"), max_hold_days=30,
        # ~p85 -- catches AAPL/MSFT-level (0.55) relatedness, most cautious
        max_correlation_threshold=Decimal("0.55"),
        max_correlation_threshold_crypto=Decimal("0.75"),
    ),
    "micro": RiskLimits(
        profile_id="micro",
        max_position_pct=Decimal("20"), max_sector_pct=Decimal("50"), min_confidence=Decimal("82"),
        max_daily_trades=2, stop_loss_pct=Decimal("6"), max_drawdown_pct=Decimal("10"),
        max_open_positions=3, max_daily_loss_pct=Decimal("2"),
        spread_limit_pct=Decimal("2"), slippage_limit_pct=Decimal("1"),
        max_risk_per_trade_pct=Decimal("1.0"), max_total_exposure_pct=Decimal("70"),
        max_simultaneous_orders=2, min_position_size_multiplier=Decimal("0.5"),
        options_premium_stop_pct=Decimal("40"), options_expiry_min_days=14, options_expiry_max_days=35,
        options_target_otm_pct=Decimal("4"), options_forced_close_days_before_expiry=2,
        break_even_trigger_pct=Decimal("3"), trailing_atr_multiplier=Decimal("2.5"), max_hold_days=12,
        # same posture as balanced -- ~p90
        max_correlation_threshold=Decimal("0.65"),
        max_correlation_threshold_crypto=Decimal("0.80"),
    ),
}


def risk_limits_for_profile(profile_id: str) -> RiskLimits:
    """Every production caller passes an already-validated Settings.risk_profile
    (Settings.from_env() rejects anything outside RISK_PROFILE_IDS before
    construction completes), so this should never see an unrecognized value
    in practice -- but it must still fail closed rather than silently
    substitute a different profile's limits for whatever was actually
    requested."""
    if profile_id not in RISK_PROFILES:
        raise SettingsError(f"unknown risk profile: {profile_id!r}")
    return RISK_PROFILES[profile_id]
