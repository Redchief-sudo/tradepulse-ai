from tradepulse.config import RISK_PROFILE_IDS, RISK_PROFILES, risk_limits_for_profile


def test_all_settings_risk_profile_ids_have_limits() -> None:
    assert set(RISK_PROFILES.keys()) == RISK_PROFILE_IDS


def test_unknown_profile_falls_back_to_balanced() -> None:
    assert risk_limits_for_profile("does-not-exist") is RISK_PROFILES["balanced"]


def test_conservative_is_stricter_than_aggressive() -> None:
    conservative = risk_limits_for_profile("conservative")
    aggressive = risk_limits_for_profile("aggressive")
    assert conservative.max_position_pct < aggressive.max_position_pct
    assert conservative.min_confidence > aggressive.min_confidence
    assert conservative.max_daily_trades < aggressive.max_daily_trades
