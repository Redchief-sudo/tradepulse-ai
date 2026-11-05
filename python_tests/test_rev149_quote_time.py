"""Rev.149 F05 (forensic audit 2026-11-04): quote age is measured when the
quote is used, not from a time taken before the awaits that fetched it.

Before the fix, execute_intent read ``now`` once at its start and
fetch_authoritative_quote compared the quote against that value after
awaiting it. A slow fetch admitted a stale quote, and a quote generated
during the fetch looked like it came from the future. Nothing rechecked the
quote before the broker POST.
"""
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from conftest import mock_cursor_read, mock_open_orders, seed_fill_cursor
from dbae_support import FullBandCapabilities
from lease_support import held_test_lease
from test_monitor_coordinator import (
    LIMITS,
    NOW,
    _mock_account,
    _mock_fill_activities,
    _mock_positions,
    _order_json,
    _seed_holding,
    _seed_lot,
)

import tradepulse.execution.gateway as gateway_module
from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, fetch_authoritative_quote
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, MarketQuote, Side
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.providers import AlpacaMarketDataProvider, ProviderDataFailure
from tradepulse.settlement import SettlementProcessor

AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


class SlowProvider:
    """Returns a quote observed at ``observed_offset`` seconds after the
    clock's start, and advances the clock ``delay`` seconds while fetching."""

    def __init__(self, clock: Clock, *, observed_offset: float, delay: float) -> None:
        self.clock, self.observed_offset, self.delay = clock, observed_offset, delay

    async def fetch_quote(self, asset):
        start = self.clock.now
        self.clock.now = start + timedelta(seconds=self.delay)
        return MarketQuote(asset=asset, price=Decimal(135), observed_at=start + timedelta(seconds=self.observed_offset),
                           received_at=self.clock.now, provider="alpaca", market_generation=1,
                           bid=Decimal("134.5"), ask=Decimal("135.5"))


async def test_a_quote_stale_on_receipt_is_refused_even_if_fresh_when_the_fetch_began() -> None:
    """The audit's probe: 20 s old when the fetch began, received 120 s
    later, so 140 s old when used (the stock limit is 30 s)."""
    clock = Clock()
    with pytest.raises(ProviderDataFailure, match="140s exceeds 30s"):
        await fetch_authoritative_quote(SlowProvider(clock, observed_offset=-20, delay=120), AAPL, Side.SELL, clock)


async def test_a_quote_generated_during_the_fetch_is_accepted() -> None:
    """The audit's probe: observed 1 s after the fetch began, received at
    +2 s. Before the fix it was refused as a negative-age quote."""
    clock = Clock()
    quote = await fetch_authoritative_quote(SlowProvider(clock, observed_offset=1, delay=2), AAPL, Side.SELL, clock)
    assert quote.price == Decimal("134.5")  # the bid, for a sell


async def test_a_quote_ahead_of_the_clock_beyond_the_skew_allowance_is_refused() -> None:
    clock = Clock()
    with pytest.raises(ProviderDataFailure, match="ahead of the local clock"):
        await fetch_authoritative_quote(SlowProvider(clock, observed_offset=10, delay=0), AAPL, Side.SELL, clock)


async def test_a_quote_ahead_of_the_clock_within_the_skew_allowance_is_accepted() -> None:
    clock = Clock()
    await fetch_authoritative_quote(SlowProvider(clock, observed_offset=1.5, delay=0), AAPL, Side.SELL, clock)


async def _gateway_setup(tmp_path, clock: Clock):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=clock)
    gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts, risk_limits_for_profile("balanced"),
                               ExecutionMode.PAPER, clock=clock, lease=held_test_lease(),
                               capabilities=FullBandCapabilities(broker, clock))
    await seed_fill_cursor(repositories)
    mock_open_orders()
    mock_cursor_read()
    return repositories, broker, market_data, gateway, alerts


def _mock_quote_at(observed) -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(return_value=httpx.Response(200, json={
        "symbol": "AAPL", "quote": {"bp": 134.5, "ap": 135.5, "t": observed.isoformat().replace("+00:00", "Z")}}))


async def _stop_exit_with_slow_dispatch(tmp_path, monkeypatch, delay_seconds: int):
    """A breached stop; ``delay_seconds`` pass during the last step before the
    broker POST: the gateway's second renewal of the symbol reservation (the
    first is the Rev.140 sell gate's, before the quote is fetched)."""
    clock = Clock()
    repositories, broker, market_data, gateway, alerts = await _gateway_setup(tmp_path, clock)
    await _seed_holding(repositories)
    await _seed_lot(repositories)
    _mock_positions(qty="10", current_price="135")  # below the 140 stop
    _mock_account()
    _mock_quote_at(NOW)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json=_order_json("filled", "10", "135")))
    _mock_fill_activities("act-1", "10", "135")
    original_renew = gateway_module.renew_lock

    renewals = 0

    async def slow_renew(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            clock.now = clock.now + timedelta(seconds=delay_seconds)
        return await original_renew(*args, **kwargs)

    monkeypatch.setattr(gateway_module, "renew_lock", slow_renew)
    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=clock)
    await broker.aclose()
    [intent_row] = await repositories.trade_intents.list_all()
    return summary, order_route, intent_row


@respx.mock
async def test_a_quote_that_goes_stale_before_dispatch_is_not_sent(tmp_path, monkeypatch) -> None:
    """Fresh when approved, 40 s old at the broker POST: the order is not
    placed, and the intent is closed so the next cycle re-authorizes."""
    summary, order_route, intent_row = await _stop_exit_with_slow_dispatch(tmp_path, monkeypatch, 40)

    assert order_route.call_count == 0
    assert summary.exits_triggered == 0
    assert intent_row["status"] == "rejected"
    assert intent_row["payload"]["rejection_reason"].startswith("STALE_EXECUTABLE_QUOTE_AT_DISPATCH")


@respx.mock
async def test_a_quote_still_fresh_at_dispatch_is_sent(tmp_path, monkeypatch) -> None:
    summary, order_route, _ = await _stop_exit_with_slow_dispatch(tmp_path, monkeypatch, 5)

    assert order_route.call_count == 1
    assert summary.exits_triggered == 1
