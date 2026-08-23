"""Symbol/side normalization for the Alpaca broker — port of the relevant
parts of base44/shared/alpaca.ts and execution.ts's defaultTimeInForce.
"""

from __future__ import annotations

import re

from tradepulse.models import AssetClass

_CRYPTO_QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BTC")
_CRYPTO_TICKER_PATTERN = re.compile(
    r"^(BTC|ETH|SOL|DOGE|AVAX|LINK|LTC|BCH|UNI|AAVE|DOT|SHIB)(USD|USDT|USDC|BTC)$"
)


def normalize_alpaca_symbol(symbol: str, asset_class: AssetClass) -> str:
    """Alpaca renders the same pair differently across market-data, order,
    activity, and position responses (e.g. BTC/USD vs BTCUSD) -- keep one
    app-side identity so fills and broker positions reconcile to the
    originating intent."""
    raw = str(symbol or "").strip().upper().replace("-", "/")
    if asset_class != AssetClass.CRYPTO or "/" in raw:
        return raw
    for quote_currency in _CRYPTO_QUOTE_CURRENCIES:
        if len(raw) > len(quote_currency) and raw.endswith(quote_currency):
            return f"{raw[: -len(quote_currency)]}/{quote_currency}"
    return raw


def infer_alpaca_asset_class(symbol: str, declared: AssetClass | None = None) -> AssetClass:
    if declared == AssetClass.CRYPTO:
        return AssetClass.CRYPTO
    raw = str(symbol or "").upper()
    if "/" in raw or "-" in raw:
        return AssetClass.CRYPTO
    return AssetClass.CRYPTO if _CRYPTO_TICKER_PATTERN.match(raw) else AssetClass.EQUITY


def default_time_in_force(asset_class: AssetClass) -> str:
    return "gtc" if asset_class == AssetClass.CRYPTO else "day"
