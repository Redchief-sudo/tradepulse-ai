"""DBAE spec §12: the equity profile ladder is retired."""
from __future__ import annotations

import pytest

from tradepulse.config import Settings, SettingsError


@pytest.mark.parametrize("value", ["true", "false", "", "0"])
def test_retired_ladder_variable_refuses_startup(value: str) -> None:
    with pytest.raises(SettingsError, match="TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY is retired"):
        Settings.from_env({"TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY": value})


def test_refusal_message_is_the_spec_text() -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_env({"TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY": "true"})
    assert str(excinfo.value) == (
        "TRADEPULSE_AUTO_RISK_PROFILE_BY_EQUITY is retired; DBAE adapts order mechanics to balance "
        "and the risk profile is always TRADEPULSE_RISK_PROFILE. Remove the variable."
    )


def test_settings_has_no_ladder_field() -> None:
    assert not hasattr(Settings.from_env({}), "auto_risk_profile_by_equity")


def test_ladder_symbols_are_gone() -> None:
    import tradepulse.cli as cli
    import tradepulse.config as config
    import tradepulse.config.risk_profiles as rp

    for name in ("EQUITY_PROFILE_LADDER", "EQUITY_PROFILE_LADDER_TOP_PROFILE_ID", "profile_id_for_equity"):
        assert not hasattr(rp, name)
        assert not hasattr(config, name)
    assert not hasattr(cli, "_resolve_risk_profile_id")
