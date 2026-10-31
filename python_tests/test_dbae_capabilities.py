from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx

from tradepulse.broker import AlpacaClient, AlpacaDataIntegrityError, AlpacaError, BrokerPort
from tradepulse.broker.capabilities import (
    AccountCapabilities,
    AccountEvidence,
    AlpacaCapabilities,
    AssetCapabilities,
    AssetTradingCapabilities,
    DailyEquityBar,
    DbaeCapabilities,
    PortfolioHistory,
    evidence_fresh,
)
from tradepulse.broker.simulated import SimulatedCapabilities
from tradepulse.models import AssetClass
from tradepulse.models.market import AssetIdentity

BASE = "https://paper-api.alpaca.markets/v2"

G0_ACCOUNT: dict[str, Any] = {
    "status": "ACTIVE", "equity": "101480.03", "cash": "50000.5", "buying_power": "400000",
    "non_marginable_buying_power": "50000.5", "multiplier": "4", "trading_blocked": False,
    "account_blocked": False, "trade_suspended_by_user": False, "crypto_status": "ACTIVE",
    "options_trading_level": 3,
}
G0_CONFIG: dict[str, Any] = {"closing_transactions_only": False, "suspend_trade": False, "fractional_trading": True}


def _mock_account(account: dict[str, Any], config: dict[str, Any] | None = None) -> None:
    respx.get(f"{BASE}/account").mock(return_value=httpx.Response(200, json=account))
    respx.get(f"{BASE}/account/configurations").mock(return_value=httpx.Response(200, json=G0_CONFIG if config is None else config))


async def _account_evidence(account: dict[str, Any], config: dict[str, Any] | None = None) -> AccountEvidence:
    _mock_account(account, config)
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        return await AlpacaCapabilities(client).get_account_evidence()
    finally:
        await client.aclose()


@respx.mock
async def test_account_evidence_parses_g0_fields() -> None:
    ev = await _account_evidence(G0_ACCOUNT)
    assert ev.status == "ACTIVE"
    assert ev.account_multiplier == Decimal(4)
    assert ev.options_trading_level == 3
    assert ev.fractional_trading is True
    assert ev.trading_blocked is False and ev.suspend_trade is False and ev.closing_transactions_only is False
    assert ev.equity == Decimal("101480.03") and ev.cash == Decimal("50000.5")
    assert ev.buying_power == Decimal(400000) and ev.non_marginable_buying_power == Decimal("50000.5")
    assert ev.crypto_status == "ACTIVE"
    assert ev.received_at.tzinfo is not None


@respx.mock
@pytest.mark.parametrize("bad", ["", None, "NaN", "Infinity", "abc", True, [], {}])
async def test_empty_and_null_numerics_are_unknown(bad: Any) -> None:
    ev = await _account_evidence({
        **G0_ACCOUNT, "non_marginable_buying_power": bad, "options_trading_level": bad, "buying_power": bad,
    })
    assert ev.non_marginable_buying_power is None
    assert ev.options_trading_level is None
    assert ev.buying_power is None
    assert ev.equity == Decimal("101480.03")


@respx.mock
async def test_missing_fields_are_unknown() -> None:
    ev = await _account_evidence({"status": "ACTIVE"}, {})
    assert ev.equity is None and ev.account_multiplier is None and ev.trading_blocked is None
    assert ev.fractional_trading is None and ev.suspend_trade is None and ev.status == "ACTIVE"


@respx.mock
async def test_wrong_types_are_unknown() -> None:
    ev = await _account_evidence(
        {**G0_ACCOUNT, "trading_blocked": "false", "account_blocked": 0, "status": 7},
        {**G0_CONFIG, "fractional_trading": "true", "suspend_trade": None},
    )
    assert ev.trading_blocked is None
    assert ev.account_blocked is None
    assert ev.status is None
    assert ev.fractional_trading is None
    assert ev.suspend_trade is None


@respx.mock
async def test_account_request_failure_raises_alpaca_error() -> None:
    respx.get(f"{BASE}/account").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    respx.get(f"{BASE}/account/configurations").mock(return_value=httpx.Response(200, json=G0_CONFIG))
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaError):
            await AlpacaCapabilities(client).get_account_evidence()
    finally:
        await client.aclose()


async def _asset(identity: AssetIdentity, url: str, body: dict[str, Any]) -> AssetTradingCapabilities:
    respx.get(url).mock(return_value=httpx.Response(200, json=body))
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        return await AlpacaCapabilities(client).get_asset_capabilities(identity)
    finally:
        await client.aclose()


@respx.mock
async def test_asset_capabilities_btc() -> None:
    ident = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "BTC/USD")
    caps = await _asset(ident, f"{BASE}/assets/{quote('BTC/USD', safe='')}", {
        "status": "active", "tradable": True, "fractionable": True,
        "min_order_size": "0.00001305", "min_trade_increment": "0.000000001", "price_increment": "1",
    })
    assert caps.min_order_size == Decimal("0.00001305")
    assert caps.min_trade_increment == Decimal("0.000000001")
    assert caps.price_increment == Decimal(1)
    assert caps.active is True and caps.tradable is True and caps.fractionable is True


@respx.mock
async def test_asset_capabilities_brk_a_not_fractionable() -> None:
    ident = AssetIdentity("BRK.A", AssetClass.EQUITY, "BRK.A")
    caps = await _asset(ident, f"{BASE}/assets/BRK.A", {"status": "active", "tradable": True, "fractionable": False})
    assert caps.fractionable is False
    assert caps.min_order_size is None and caps.min_trade_increment is None and caps.price_increment is None


@respx.mock
async def test_asset_wrong_types_and_inactive() -> None:
    ident = AssetIdentity("AAPL", AssetClass.EQUITY, "AAPL")
    caps = await _asset(ident, f"{BASE}/assets/AAPL", {"status": "inactive", "tradable": "true", "fractionable": None})
    assert caps.active is False
    assert caps.tradable is None and caps.fractionable is None


@respx.mock
async def test_option_capabilities_are_whole_contract_facts() -> None:
    ident = AssetIdentity("AAPL261120C00200000", AssetClass.OPTION, "AAPL261120C00200000")
    caps = await _asset(ident, f"{BASE}/options/contracts/AAPL261120C00200000", {"status": "active", "tradable": True})
    assert caps.active is True and caps.tradable is True
    assert caps.fractionable is False
    assert caps.min_order_size is None and caps.min_trade_increment is None


@respx.mock
async def test_asset_request_failure_raises_alpaca_error() -> None:
    respx.get(f"{BASE}/assets/ZZZ").mock(return_value=httpx.Response(404, json={"message": "nope"}))
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaError):
            await AlpacaCapabilities(client).get_asset_capabilities(AssetIdentity("ZZZ", AssetClass.EQUITY, "ZZZ"))
    finally:
        await client.aclose()


# G1 evidence (spec 13.1): bars stamped 00:00 UTC the day after each trading date.
G1_TS = [1792454400, 1793318400]  # 2026-10-20T00:00Z, 2026-10-30T00:00Z


async def _bars(body: dict[str, Any]) -> list[DailyEquityBar]:
    respx.get(f"{BASE}/account/portfolio/history", params={"period": "1M", "timeframe": "1D"}).mock(
        return_value=httpx.Response(200, json=body))
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        return await AlpacaCapabilities(client).get_daily_equity_bars()
    finally:
        await client.aclose()


@respx.mock
async def test_daily_bars_map_by_new_york_date() -> None:
    bars = await _bars({"timestamp": [1793318400], "equity": [101480.03], "timeframe": "1D"})
    assert bars == [DailyEquityBar(trading_date=date(2026, 10, 29), equity=Decimal("101480.03"))]


@respx.mock
async def test_daily_bars_drop_bad_equity_and_sort() -> None:
    ts = [1793318400, 1793232000, 1793145600, 1793059200, 1792972800]
    bars = await _bars({"timestamp": ts, "equity": [101480.03, None, -5, "NaN", 101422.2]})
    assert [b.trading_date for b in bars] == [date(2026, 10, 25), date(2026, 10, 29)]
    assert bars[0].equity == Decimal("101422.2")


@respx.mock
async def test_daily_bars_mismatched_arrays_raise() -> None:
    respx.get(f"{BASE}/account/portfolio/history").mock(
        return_value=httpx.Response(200, json={"timestamp": [1, 2], "equity": [1]}))
    client = AlpacaClient("key", "secret", "paper", 10)
    try:
        with pytest.raises(AlpacaDataIntegrityError):
            await AlpacaCapabilities(client).get_daily_equity_bars()
    finally:
        await client.aclose()


def test_evidence_fresh_bounds() -> None:
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    assert evidence_fresh(now - timedelta(seconds=30), now)
    assert evidence_fresh(now + timedelta(seconds=5), now)
    assert not evidence_fresh(now - timedelta(seconds=31), now)
    assert not evidence_fresh(now + timedelta(seconds=6), now)


def test_broker_port_unchanged() -> None:
    # The BrokerPort member set at the DBAE base commit (DBAE-03: no new port methods).
    assert set(BrokerPort.__protocol_attrs__) == {
        "venue", "implementation", "get_account", "get_positions", "get_clock", "get_calendar",
        "get_dividend_announcements", "place_order", "get_order", "get_order_by_client_order_id",
        "get_open_orders", "cancel_order", "get_activities", "get_latest_quote", "get_latest_option_quotes",
        "get_options_chain", "get_bars", "set_market_data_feeds", "rate_limit_snapshot", "aclose",
    }


async def test_simulated_capabilities_satisfy_dbae_protocols() -> None:
    sim = SimulatedCapabilities()
    assert isinstance(sim, AccountCapabilities) and isinstance(sim, AssetCapabilities)
    assert isinstance(sim, PortfolioHistory)
    dbae: DbaeCapabilities = sim
    assert await dbae.get_daily_equity_bars() == []
    ident = AssetIdentity("AAPL", AssetClass.EQUITY, "AAPL")
    now = datetime.now(UTC)
    sim.assets["AAPL"] = AssetTradingCapabilities(True, True, True, None, None, None, now)
    assert (await dbae.get_asset_capabilities(ident)).fractionable is True
    with pytest.raises(AlpacaError):
        await dbae.get_asset_capabilities(AssetIdentity("ZZZ", AssetClass.EQUITY, "ZZZ"))
    with pytest.raises(AlpacaError):
        await dbae.get_account_evidence()
