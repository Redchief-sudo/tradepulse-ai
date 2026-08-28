from __future__ import annotations

from dataclasses import dataclass
from os import environ
from typing import Mapping


class SettingsError(ValueError):
    """Raised when runtime configuration is unsafe or incomplete."""


def _int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise SettingsError(f"{name} must be a boolean")
    return value in {"true", "1", "yes"}


RISK_PROFILE_IDS = frozenset({"aggressive", "balanced", "conservative", "micro"})
AI_PROVIDER_IDS = frozenset({"anthropic", "openai"})


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    log_level: str
    execution_mode: str
    live_trading_enabled: bool
    market_data_timeout_seconds: int
    broker_timeout_seconds: int
    ai_timeout_seconds: int
    max_retry_attempts: int
    alpaca_api_key: str | None
    alpaca_api_secret: str | None
    alpaca_base_url: str
    finnhub_api_key: str | None
    coingecko_api_key: str | None
    coinmarketcap_api_key: str | None
    ai_provider: str
    anthropic_api_key: str | None
    anthropic_base_url: str | None
    anthropic_model: str
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    api_signing_secret: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    risk_profile: str
    equity_universe_path: str | None
    crypto_universe_path: str | None
    options_universe_path: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = environ if env is None else env
        mode = values.get("TRADEPULSE_EXECUTION_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise SettingsError("TRADEPULSE_EXECUTION_MODE must be paper or live")
        live_enabled = _bool(values, "TRADEPULSE_LIVE_TRADING_ENABLED", False)
        alpaca_key = values.get("ALPACA_API_KEY") or None
        alpaca_secret = values.get("ALPACA_API_SECRET") or None
        if mode == "live" and not live_enabled:
            raise SettingsError("live mode requires TRADEPULSE_LIVE_TRADING_ENABLED=true")
        if mode == "live" and (not alpaca_key or not alpaca_secret):
            raise SettingsError("live mode requires ALPACA_API_KEY and ALPACA_API_SECRET")
        risk_profile = values.get("TRADEPULSE_RISK_PROFILE", "balanced").strip().lower()
        if risk_profile not in RISK_PROFILE_IDS:
            raise SettingsError(f"TRADEPULSE_RISK_PROFILE must be one of {sorted(RISK_PROFILE_IDS)}")
        ai_provider = values.get("TRADEPULSE_AI_PROVIDER", "anthropic").strip().lower()
        if ai_provider not in AI_PROVIDER_IDS:
            raise SettingsError(f"TRADEPULSE_AI_PROVIDER must be one of {sorted(AI_PROVIDER_IDS)}")
        return cls(
            database_url=values.get("TRADEPULSE_DATABASE_URL", "sqlite:///tradepulse.db"),
            log_level=values.get("TRADEPULSE_LOG_LEVEL", "INFO").upper(),
            execution_mode=mode,
            live_trading_enabled=live_enabled,
            market_data_timeout_seconds=_int(values, "MARKET_DATA_TIMEOUT_SECONDS", 10, 1),
            broker_timeout_seconds=_int(values, "BROKER_TIMEOUT_SECONDS", 15, 1),
            ai_timeout_seconds=_int(values, "AI_TIMEOUT_SECONDS", 45, 1),
            max_retry_attempts=_int(values, "MAX_RETRY_ATTEMPTS", 3, 1),
            alpaca_api_key=alpaca_key,
            alpaca_api_secret=alpaca_secret,
            alpaca_base_url=values.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            finnhub_api_key=values.get("FINNHUB_API_KEY") or None,
            coingecko_api_key=values.get("COINGECKO_API_KEY") or None,
            coinmarketcap_api_key=values.get("COINMARKETCAP_API_KEY") or None,
            ai_provider=ai_provider,
            anthropic_api_key=values.get("ANTHROPIC_API_KEY") or None,
            anthropic_base_url=values.get("ANTHROPIC_BASE_URL") or None,
            anthropic_model=values.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            openai_api_key=values.get("OPENAI_API_KEY") or None,
            openai_base_url=values.get("OPENAI_BASE_URL") or None,
            openai_model=values.get("OPENAI_MODEL", "gpt-4o-mini"),
            api_signing_secret=values.get("TRADEPULSE_API_SIGNING_SECRET") or None,
            telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=values.get("TELEGRAM_CHAT_ID") or None,
            risk_profile=risk_profile,
            equity_universe_path=values.get("TRADEPULSE_EQUITY_UNIVERSE_PATH") or None,
            crypto_universe_path=values.get("TRADEPULSE_CRYPTO_UNIVERSE_PATH") or None,
            options_universe_path=values.get("TRADEPULSE_OPTIONS_UNIVERSE_PATH") or None,
        )
