import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient, AlpacaError, is_definitive_rejection
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
