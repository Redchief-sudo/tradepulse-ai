from dataclasses import fields, replace
from decimal import Decimal

import pytest

from tradepulse.config import (
    RISK_PROFILE_IDS,
    RISK_PROFILES,
    SettingsError,
    profile_id_for_equity,
    risk_limits_for_profile,
)
from tradepulse.models import RiskLimits


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


# ---- Rev.141: every profile sets the Phase 1 entry limits ----------------------

ENTRY_LIMIT_FIELDS = ("max_open_risk_pct", "max_entries_per_symbol_per_day", "max_cost_to_risk_pct")

# Operator-approved values (Rev.141): (combined open risk %, entries per symbol per day, cost as % of R).
APPROVED_ENTRY_LIMITS = {
    "conservative": (Decimal("1.0"), 1, Decimal("8")),
    "micro": (Decimal("3.0"), 1, Decimal("10")),
    "balanced": (Decimal("3"), 2, Decimal("10")),
    "aggressive": (Decimal("5.0"), 2, Decimal("10")),
}


def test_approved_entry_limits_cover_every_profile() -> None:
    assert set(APPROVED_ENTRY_LIMITS) == RISK_PROFILE_IDS


@pytest.mark.parametrize("profile_id", sorted(RISK_PROFILE_IDS))
def test_every_profile_sets_the_approved_entry_limits(profile_id: str) -> None:
    limits = risk_limits_for_profile(profile_id)
    assert tuple(getattr(limits, name) for name in ENTRY_LIMIT_FIELDS) == APPROVED_ENTRY_LIMITS[profile_id]


@pytest.mark.parametrize("field", ENTRY_LIMIT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, "0", "not-a-number"])
def test_entry_limits_reject_zero_negative_and_invalid_values(field: str, value) -> None:
    """A zero used to switch the check off. Now it is a configuration error."""
    with pytest.raises(ValueError, match=field):
        replace(risk_limits_for_profile("balanced"), **{field: value})


@pytest.mark.parametrize("omitted", ENTRY_LIMIT_FIELDS)
def test_entry_limits_cannot_be_omitted(omitted: str) -> None:
    balanced = risk_limits_for_profile("balanced")
    kwargs = {f.name: getattr(balanced, f.name) for f in fields(balanced) if f.name != omitted}
    with pytest.raises(ValueError, match=omitted):
        RiskLimits(**kwargs)
