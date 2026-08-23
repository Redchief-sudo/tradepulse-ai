import pytest

from tradepulse.config import Settings, SettingsError


def test_paper_defaults_do_not_require_credentials() -> None:
    settings = Settings.from_env({})
    assert settings.execution_mode == "paper"
    assert not settings.live_trading_enabled
    assert settings.alpaca_api_key is None


def test_live_mode_requires_explicit_enable_and_credentials() -> None:
    with pytest.raises(SettingsError, match="LIVE_TRADING_ENABLED"):
        Settings.from_env({"TRADEPULSE_EXECUTION_MODE": "live"})
    with pytest.raises(SettingsError, match="ALPACA_API_KEY"):
        Settings.from_env({"TRADEPULSE_EXECUTION_MODE": "live", "TRADEPULSE_LIVE_TRADING_ENABLED": "true"})
    settings = Settings.from_env({
        "TRADEPULSE_EXECUTION_MODE": "live",
        "TRADEPULSE_LIVE_TRADING_ENABLED": "true",
        "ALPACA_API_KEY": "key",
        "ALPACA_API_SECRET": "secret",
    })
    assert settings.execution_mode == "live"


def test_invalid_timeout_fails_configuration() -> None:
    with pytest.raises(SettingsError, match="MARKET_DATA_TIMEOUT_SECONDS"):
        Settings.from_env({"MARKET_DATA_TIMEOUT_SECONDS": "0"})


def test_defaults_use_balanced_risk_profile_and_anthropic_haiku() -> None:
    settings = Settings.from_env({})
    assert settings.risk_profile == "balanced"
    assert settings.anthropic_model == "claude-haiku-4-5-20251001"
    assert settings.anthropic_api_key is None
    assert settings.telegram_bot_token is None


def test_unknown_risk_profile_fails_configuration() -> None:
    with pytest.raises(SettingsError, match="TRADEPULSE_RISK_PROFILE"):
        Settings.from_env({"TRADEPULSE_RISK_PROFILE": "yolo"})


def test_risk_profile_and_anthropic_overrides_are_honored() -> None:
    settings = Settings.from_env({
        "TRADEPULSE_RISK_PROFILE": "conservative",
        "ANTHROPIC_API_KEY": "key",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_CHAT_ID": "12345",
    })
    assert settings.risk_profile == "conservative"
    assert settings.anthropic_api_key == "key"
    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.telegram_bot_token == "bot-token"
    assert settings.telegram_chat_id == "12345"
