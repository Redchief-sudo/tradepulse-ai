from decimal import Decimal

import pytest

from tradepulse.config import (
    RISK_PROFILE_IDS, RISK_PROFILES, SettingsError, profile_id_for_equity, risk_limits_for_profile,
)


def test_all_settings_risk_profile_ids_have_limits() -> None:
    assert set(RISK_PROFILES.keys()) == RISK_PROFILE_IDS


def test_unknown_profile_fails_closed() -> None:
    """risk_limits_for_profile must never silently substitute a different
    profile's limits for an unrecognized id -- every production caller
    already passes an already-validated Settings.risk_profile, so this is
    a defensive fail-closed guard, not a live bypass path."""
    with pytest.raises(SettingsError, match="does-not-exist"):
        risk_limits_for_profile("does-not-exist")


def test_conservative_is_stricter_than_aggressive() -> None:
    conservative = risk_limits_for_profile("conservative")
    aggressive = risk_limits_for_profile("aggressive")
    assert conservative.max_position_pct < aggressive.max_position_pct
    assert conservative.min_confidence > aggressive.min_confidence
    assert conservative.max_daily_trades < aggressive.max_daily_trades


@pytest.mark.parametrize(
    "equity,expected_profile",
    [
        (Decimal("0"), "micro"),
        (Decimal("9999.99"), "micro"),
        (Decimal("10000"), "aggressive"),  # lower bound is inclusive on the tier it enters
        (Decimal("49999.99"), "aggressive"),
        (Decimal("50000"), "balanced"),
        (Decimal("249999.99"), "balanced"),
        (Decimal("250000"), "conservative"),
        (Decimal("10000000"), "conservative"),
    ],
)
def test_profile_id_for_equity_ladder_boundaries(equity: Decimal, expected_profile: str) -> None:
    """Every boundary is exercised on both sides -- a fence-post error here
    would silently pick the wrong risk profile for real account equity."""
    assert profile_id_for_equity(equity) == expected_profile


def test_profile_id_for_equity_result_always_a_real_profile() -> None:
    """Whatever the ladder returns must resolve through risk_limits_for_profile
    without raising -- a typo in EQUITY_PROFILE_LADDER/EQUITY_PROFILE_LADDER_TOP_PROFILE_ID
    would otherwise only surface as a live SettingsError mid-cycle."""
    for equity in (Decimal("-1"), Decimal("0"), Decimal("10000"), Decimal("50000"), Decimal("250000"), Decimal("1e12")):
        risk_limits_for_profile(profile_id_for_equity(equity))  # must not raise
