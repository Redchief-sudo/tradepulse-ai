import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.providers import MarketDataCapabilities, MarketDataCapabilityError, resolve_market_data_capabilities


def _mock_sip_quote(status: int = 200) -> respx.Route:
    if status == 200:
        return respx.get("https://data.alpaca.markets/v2/stocks/SPY/quotes/latest").mock(
            return_value=httpx.Response(200, json={"symbol": "SPY", "quote": {"bp": 550.0, "ap": 550.10, "t": "2026-08-27T15:00:00Z"}})
        )
    return respx.get("https://data.alpaca.markets/v2/stocks/SPY/quotes/latest").mock(
        return_value=httpx.Response(status, json={"message": "forbidden" if status == 403 else "error"})
    )


def _mock_chain(occ_symbol: str = "SPY261016C00550000") -> respx.Route:
    return respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(200, json={
            "option_contracts": [{
                "symbol": occ_symbol, "underlying_symbol": "SPY", "type": "call", "strike_price": "550",
                "expiration_date": "2026-10-16", "multiplier": "100", "status": "active", "tradable": True,
            }],
            "next_page_token": None,
        })
    )


def _mock_opra_quote(occ_symbol: str = "SPY261016C00550000", status: int = 200) -> respx.Route:
    if status == 200:
        return respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
            return_value=httpx.Response(200, json={"quotes": {occ_symbol: {"bp": 5.0, "ap": 5.10, "t": "2026-08-27T15:00:00Z"}}})
        )
    return respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(status, json={"message": "forbidden" if status == 403 else "error"})
    )


def _client() -> AlpacaClient:
    return AlpacaClient("key", "secret", "paper", 10)


@respx.mock
async def test_basic_tier_forces_free_feeds_with_no_http_calls() -> None:
    broker = _client()
    try:
        capabilities = await resolve_market_data_capabilities(broker, "basic")
    finally:
        await broker.aclose()
    assert capabilities == MarketDataCapabilities("iex", "indicative")
    assert capabilities.tier_label == "basic"
    # respx has no mocks registered at all -- an unexpected call would fail
    # this test via AllMockedAssertionError, proving no probe happened.


@respx.mock
async def test_auto_both_probes_succeed_resolves_to_algo_trader_plus() -> None:
    _mock_sip_quote(200)
    _mock_chain()
    _mock_opra_quote(status=200)
    broker = _client()
    try:
        capabilities = await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert capabilities == MarketDataCapabilities("sip", "opra")
    assert capabilities.tier_label == "algo_trader_plus"


@respx.mock
async def test_auto_both_probes_rejected_resolves_to_basic() -> None:
    _mock_sip_quote(403)
    _mock_chain()
    _mock_opra_quote(status=403)
    broker = _client()
    try:
        capabilities = await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert capabilities == MarketDataCapabilities("iex", "indicative")
    assert capabilities.tier_label == "basic"


@respx.mock
async def test_auto_mixed_capability_resolved_independently() -> None:
    """SIP rejected, OPRA entitled -- proves the two feeds are resolved
    fully independently, not coupled to one combined check."""
    _mock_sip_quote(403)
    _mock_chain()
    _mock_opra_quote(status=200)
    broker = _client()
    try:
        capabilities = await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert capabilities == MarketDataCapabilities("iex", "opra")
    assert capabilities.tier_label == "mixed:equity=iex,option=opra"


@respx.mock
async def test_auto_authentication_failure_propagates_never_read_as_basic() -> None:
    _mock_sip_quote(401)
    broker = _client()
    try:
        with pytest.raises(AlpacaError) as exc_info:
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert exc_info.value.status_code == 401


@respx.mock
async def test_auto_rate_limit_propagates() -> None:
    _mock_sip_quote(429)
    broker = _client()
    try:
        with pytest.raises(AlpacaError) as exc_info:
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert exc_info.value.status_code == 429


@respx.mock
async def test_auto_server_error_propagates() -> None:
    _mock_sip_quote(500)
    broker = _client()
    try:
        with pytest.raises(AlpacaError):
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()


@respx.mock
async def test_auto_transport_failure_propagates() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/quotes/latest").mock(side_effect=httpx.ConnectError("connection refused"))
    broker = _client()
    try:
        with pytest.raises(httpx.ConnectError):
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()


@respx.mock
async def test_auto_missing_bid_ask_is_indeterminate_not_basic() -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "SPY", "quote": {}})
    )
    broker = _client()
    try:
        with pytest.raises(MarketDataCapabilityError):
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()


@respx.mock
async def test_algo_trader_plus_both_probes_succeed() -> None:
    _mock_sip_quote(200)
    _mock_chain()
    _mock_opra_quote(status=200)
    broker = _client()
    try:
        capabilities = await resolve_market_data_capabilities(broker, "algo_trader_plus")
    finally:
        await broker.aclose()
    assert capabilities == MarketDataCapabilities("sip", "opra")


@respx.mock
async def test_algo_trader_plus_required_but_opra_rejected_raises() -> None:
    """The strict/required-premium policy: unlike 'auto', this must fail
    startup rather than gracefully downgrading."""
    _mock_sip_quote(200)
    _mock_chain()
    _mock_opra_quote(status=403)
    broker = _client()
    try:
        with pytest.raises(MarketDataCapabilityError, match="OPRA"):
            await resolve_market_data_capabilities(broker, "algo_trader_plus")
    finally:
        await broker.aclose()


@respx.mock
async def test_algo_trader_plus_required_but_sip_rejected_raises() -> None:
    _mock_sip_quote(403)
    _mock_chain()
    _mock_opra_quote(status=200)
    broker = _client()
    try:
        with pytest.raises(MarketDataCapabilityError, match="SIP"):
            await resolve_market_data_capabilities(broker, "algo_trader_plus")
    finally:
        await broker.aclose()


@respx.mock
async def test_opra_probe_uses_a_real_contract_from_the_chain_not_a_placeholder() -> None:
    _mock_sip_quote(200)
    _mock_chain(occ_symbol="SPY261016C00550000")
    opra_route = _mock_opra_quote(occ_symbol="SPY261016C00550000", status=200)
    broker = _client()
    try:
        await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
    assert "SPY261016C00550000" in str(opra_route.calls[0].request.url)


@respx.mock
async def test_auto_empty_options_chain_propagates() -> None:
    _mock_sip_quote(200)
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(200, json={"option_contracts": [], "next_page_token": None})
    )
    broker = _client()
    try:
        with pytest.raises(MarketDataCapabilityError):
            await resolve_market_data_capabilities(broker, "auto")
    finally:
        await broker.aclose()
