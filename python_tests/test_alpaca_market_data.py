from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient
from tradepulse.models import AssetClass, AssetIdentity
from tradepulse.providers import AlpacaMarketDataProvider, ProviderDataFailure

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


def _provider() -> AlpacaMarketDataProvider:
    return AlpacaMarketDataProvider(AlpacaClient("key", "secret", "paper", 10))


def _bars_json(closes: list[float]) -> dict:
    end = NOW
    rows = []
    for offset, close in enumerate(closes):
        day = end - timedelta(days=len(closes) - 1 - offset)
        rows.append({
            "t": day.isoformat().replace("+00:00", "Z"), "o": close * 0.998, "h": close * 1.006,
            "l": close * 0.994, "c": close, "v": 1_000_000.0,
        })
    return {"bars": list(reversed(rows))}  # newest-first, matching the client's sort=desc request


@respx.mock
async def test_fetch_candles_raises_provider_data_failure_on_malformed_numeric_field() -> None:
    """Rev.83 REL-001: broker/alpaca_client.py::get_bars's own raw-bar
    parsing is bare Decimal(str(value)) -- a non-numeric field used to
    raise an unguarded decimal.InvalidOperation straight out of
    fetch_candles, which every caller's `except ProviderError` would NOT
    have caught. Now normalized into ProviderDataFailure at the provider
    boundary."""
    bars = _bars_json([150.0] * 40)
    bars["bars"][0]["h"] = "not-a-number"
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(return_value=httpx.Response(200, json=bars))

    with pytest.raises(ProviderDataFailure) as exc_info:
        await _provider().fetch_candles(_aapl())
    assert exc_info.value.error_code == "ALPACA_BAR_MALFORMED"


@respx.mock
async def test_fetch_candles_raises_provider_data_failure_on_semantically_invalid_bar() -> None:
    """Candle.__post_init__ raises for a bar where high < low -- also now
    normalized into ProviderDataFailure rather than an unguarded
    ValueError/DomainValidationError."""
    bars = _bars_json([150.0] * 40)
    bars["bars"][0]["h"] = 100.0  # high below low
    bars["bars"][0]["l"] = 200.0
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(return_value=httpx.Response(200, json=bars))

    with pytest.raises(ProviderDataFailure) as exc_info:
        await _provider().fetch_candles(_aapl())
    assert exc_info.value.error_code == "ALPACA_BAR_INVALID"


@respx.mock
async def test_fetch_candles_still_succeeds_on_well_formed_bars() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json=_bars_json([150.0 + i for i in range(40)]))
    )

    candles = await _provider().fetch_candles(_aapl())

    assert len(candles) == 40
