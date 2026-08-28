import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient, AlpacaDataIntegrityError, AlpacaError, default_time_in_force, is_definitive_rejection
from tradepulse.models import AssetClass


def _crypto_bar(day: "datetime") -> dict:
    return {"t": day.isoformat().replace("+00:00", "Z"), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 10.0}


@respx.mock
async def test_get_bars_returns_most_recent_bars_not_oldest_when_range_exceeds_limit() -> None:
    """Regression test for a confirmed live-verification defect: requesting
    ascending-sorted bars with a limit smaller than the number of bars
    available in the date range silently returns the OLDEST `limit` bars,
    not the most recent ones. This was caught live: BTC/USD candles (which
    trade every calendar day, unlike weekdays-only equities) stopped ~2.5
    months before "today" under the old ascending-sort request. The fix
    requests descending order (most-recent-first) up to `limit`, then
    reverses to ascending -- this test proves the most recent day survives
    when there are more days in the window than the limit allows.
    """
    end = datetime(2026, 8, 22, tzinfo=UTC)
    all_days = [end - timedelta(days=i) for i in range(300)]  # 300 calendar days, most-recent-first
    limit = 250

    def handler(request: httpx.Request) -> httpx.Response:
        # Alpaca's real API returns the page matching the requested sort
        # order; the client asked for desc, so serve most-recent-first,
        # truncated to `limit` -- exactly what a real "not enough limit"
        # scenario looks like.
        rows = [_crypto_bar(d) for d in all_days[:limit]]
        return httpx.Response(200, json={"bars": {"BTC/USD": rows}, "next_page_token": None})

    respx.get("https://data.alpaca.markets/v1beta3/crypto/us/bars").mock(side_effect=handler)

    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        bars = await client.get_bars("BTC/USD", AssetClass.CRYPTO, end - timedelta(days=330), end, limit=limit)
    finally:
        await client.aclose()

    assert len(bars) == limit
    assert bars[-1].date == end.date().isoformat()  # most recent day must survive
    assert bars[0].date < bars[-1].date  # still returned oldest-first


@respx.mock
async def test_get_account_parses_equity_and_cash() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(
            200, json={"equity": "100000.00", "last_equity": "99500.00", "cash": "50000.00", "buying_power": "200000.00", "portfolio_value": "100000.00"}
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        account = await client.get_account()
    finally:
        await client.aclose()
    assert account.equity == 100000
    assert account.cash == 50000


@respx.mock
async def test_get_positions_normalizes_crypto_symbols() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSD", "asset_class": "crypto", "qty": "0.5", "avg_entry_price": "60000",
                    "market_value": "32500", "current_price": "65000", "unrealized_pl": "2500",
                },
                {
                    "symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
                    "market_value": "1550", "current_price": "155", "unrealized_pl": "50",
                },
            ],
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        positions = await client.get_positions()
    finally:
        await client.aclose()
    by_symbol = {p.symbol: p for p in positions}
    assert "BTC/USD" in by_symbol  # normalized from Alpaca's raw "BTCUSD"
    assert by_symbol["BTC/USD"].asset_class == AssetClass.CRYPTO
    assert "AAPL" in by_symbol
    assert by_symbol["AAPL"].asset_class == AssetClass.EQUITY


@respx.mock
async def test_get_positions_fails_closed_on_unrecognized_asset_class() -> None:
    """A position with an asset_class this system doesn't recognize must
    never be silently coerced into EQUITY -- that would build a WRONG
    canonical identity for a real broker position and corrupt every local
    lookup keyed by it (Holding, protective-exit classification,
    reconciliation)."""
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "symbol": "WEIRD", "asset_class": "some_future_asset_class", "qty": "1", "avg_entry_price": "1",
                "market_value": "0", "current_price": "1", "unrealized_pl": "0",
            }],
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaDataIntegrityError, match="BROKER_ASSET_CLASS_UNKNOWN"):
            await client.get_positions()
    finally:
        await client.aclose()


@respx.mock
async def test_get_positions_infers_options_asset_class_from_us_option() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL251219C00150000", "asset_class": "us_option", "qty": "1", "avg_entry_price": "2.00",
                    "market_value": "250", "current_price": "2.50", "unrealized_pl": "50",
                },
            ],
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        positions = await client.get_positions()
    finally:
        await client.aclose()
    assert positions[0].symbol == "AAPL251219C00150000"
    assert positions[0].asset_class == AssetClass.OPTION


@respx.mock
async def test_get_latest_quote_parses_options_envelope() -> None:
    route = respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(
            200, json={"quotes": {"AAPL251219C00150000": {"bp": 2.0, "ap": 2.1, "t": "2026-08-26T15:00:00Z"}}}
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        quote = await client.get_latest_quote("AAPL251219C00150000", AssetClass.OPTION)
    finally:
        await client.aclose()
    assert quote.bid == Decimal("2.0")
    assert quote.ask == Decimal("2.1")
    assert quote.source == "alpaca_options"
    # feed is explicit (opra), never left to Alpaca's own "opra if
    # subscribed, else indicative" default -- a silent downgrade to a
    # delayed/modified feed must never happen unnoticed.
    assert route.calls[0].request.url.params["feed"] == "opra"


@respx.mock
async def test_get_latest_quote_fails_closed_when_opra_not_authorized() -> None:
    """An account without OPRA access gets a definitive 403 -- this must
    propagate as a real error, never a silent fallback to a cheaper feed."""
    respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(403, json={"message": "not subscribed to OPRA", "code": 40410000})
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaError) as exc_info:
            await client.get_latest_quote("AAPL251219C00150000", AssetClass.OPTION)
    finally:
        await client.aclose()
    assert exc_info.value.status_code == 403
    assert exc_info.value.is_auth_error()


@respx.mock
async def test_get_options_chain_parses_contracts_and_skips_non_tradable() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(
            200,
            json={
                "option_contracts": [
                    {
                        "symbol": "aapl251219c00150000", "underlying_symbol": "aapl", "type": "call",
                        "strike_price": "150.00", "expiration_date": "2025-12-19", "multiplier": "100",
                        "status": "active", "tradable": True,
                    },
                    {
                        "symbol": "AAPL251219C00160000", "underlying_symbol": "AAPL", "type": "call",
                        "strike_price": "160.00", "expiration_date": "2025-12-19", "multiplier": "100",
                        "status": "active", "tradable": False,
                    },
                ],
                "next_page_token": None,
            },
        )
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        contracts = await client.get_options_chain("AAPL", "2025-12-01", "2025-12-31")
    finally:
        await client.aclose()
    assert len(contracts) == 1  # the non-tradable contract is skipped
    assert contracts[0].occ_symbol == "AAPL251219C00150000"  # uppercased
    assert contracts[0].underlying_symbol == "AAPL"
    assert contracts[0].strike_price == Decimal("150.00")
    assert contracts[0].multiplier == Decimal("100")


@respx.mock
async def test_get_options_chain_follows_pagination() -> None:
    page1 = httpx.Response(200, json={
        "option_contracts": [
            {"symbol": "AAPL251219C00150000", "underlying_symbol": "AAPL", "type": "call", "strike_price": "150",
             "expiration_date": "2025-12-19", "multiplier": "100", "status": "active", "tradable": True},
        ],
        "next_page_token": "page-2",
    })
    page2 = httpx.Response(200, json={
        "option_contracts": [
            {"symbol": "AAPL251219C00155000", "underlying_symbol": "AAPL", "type": "call", "strike_price": "155",
             "expiration_date": "2025-12-19", "multiplier": "100", "status": "active", "tradable": True},
        ],
        "next_page_token": None,
    })
    route = respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(side_effect=[page1, page2])
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        contracts = await client.get_options_chain("AAPL", "2025-12-01", "2025-12-31")
    finally:
        await client.aclose()
    assert route.call_count == 2
    assert {c.occ_symbol for c in contracts} == {"AAPL251219C00150000", "AAPL251219C00155000"}


def test_default_time_in_force_is_day_for_options() -> None:
    assert default_time_in_force(AssetClass.OPTION) == "day"


@respx.mock
async def test_non_success_response_raises_typed_alpaca_error() -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(403, json={"message": "insufficient buying power", "code": 40310000})
    )
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaError) as exc_info:
            await client.get_account()
    finally:
        await client.aclose()
    assert exc_info.value.status_code == 403
    assert exc_info.value.is_insufficient_buying_power()


def _alpaca_error(status_code: int) -> AlpacaError:
    return AlpacaError("boom", status_code, "req-1", None, "placeOrder")


def test_is_definitive_rejection_true_for_a_clear_4xx_business_rejection() -> None:
    assert is_definitive_rejection(_alpaca_error(422)) is True
    assert is_definitive_rejection(_alpaca_error(403)) is True


def test_is_definitive_rejection_false_for_rate_limit() -> None:
    assert is_definitive_rejection(_alpaca_error(429)) is False


def test_is_definitive_rejection_false_for_server_errors() -> None:
    assert is_definitive_rejection(_alpaca_error(500)) is False
    assert is_definitive_rejection(_alpaca_error(503)) is False


def test_is_definitive_rejection_false_below_400() -> None:
    """A non-2xx response below 400 (e.g. a redirect an intermediary
    surfaced as non-success) is not a 4xx business-logic rejection --
    the docstring's own contract requires 400 <= status_code < 500."""
    assert is_definitive_rejection(_alpaca_error(302)) is False
    assert is_definitive_rejection(_alpaca_error(100)) is False


def test_is_definitive_rejection_false_for_non_alpaca_exceptions() -> None:
    assert is_definitive_rejection(httpx.ConnectError("connection refused")) is False
    assert is_definitive_rejection(httpx.TimeoutException("timed out")) is False


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("ALPACA_API_KEY"), reason="requires real ALPACA_API_KEY/SECRET in the environment")
async def test_live_paper_account_clock_is_reachable() -> None:
    client = AlpacaClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"], "paper", 10)
    try:
        clock = await client.get_clock()
    finally:
        await client.aclose()
    assert isinstance(clock.is_open, bool)
