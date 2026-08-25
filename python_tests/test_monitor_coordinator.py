import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Holding,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.settlement import SettlementProcessor


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _setup(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    limits = risk_limits_for_profile("balanced")
    gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts, limits, ExecutionMode.PAPER, clock=lambda: NOW)
    return repositories, broker, gateway, alerts


def _positions_json(qty: str, current_price: str, avg_entry: str = "150") -> list[dict]:
    return [
        {
            "symbol": "AAPL", "asset_class": "us_equity", "qty": qty, "avg_entry_price": avg_entry,
            "market_value": "0", "current_price": current_price, "unrealized_pl": "0",
        }
    ]


def _mock_positions(qty: str, current_price: str) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(200, json=_positions_json(qty, current_price))
    )


def _mock_account(cash: str = "50000", equity: str = "100000") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": "99500", "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_quote(bid: str = "134.50", ask: str = "135.50") -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None, side: str = "sell") -> dict:
    return {
        "id": "order-1", "status": status, "symbol": "AAPL", "side": side,
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


def _mock_fill_activities(activity_id: str, qty: str, price: str, side: str = "sell", order_id: str = "order-1") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": side,
                "qty": qty, "price": price, "transaction_time": QUOTE_TS, "order_id": order_id,
            }],
        )
    )


async def _seed_holding(repositories: PersistenceRepositories, *, quantity: str = "10", stop_loss: str | None = "140", target_price: str | None = "170") -> None:
    holding = Holding(
        asset=_aapl(), quantity=Decimal(quantity), average_price=Decimal("150"), updated_at=NOW,
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        target_price=Decimal(target_price) if target_price else None,
    )
    await repositories.holdings.create_once("AAPL", holding)


@respx.mock
async def test_long_position_breaching_stop_triggers_sell(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # below stop_loss=140
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "135")))
    _mock_fill_activities("act-1", "10", "135")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "ok"
    assert summary.positions_checked == 1
    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    request_body = order_route.calls[0].request.content
    assert b'"side":"sell"' in request_body
    assert b'"qty":"10"' in request_body


@respx.mock
async def test_long_position_breaching_target_triggers_sell(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="175")  # above target_price=170
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "175")))
    _mock_fill_activities("act-1", "10", "175")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1


@respx.mock
async def test_short_position_breaching_inverted_thresholds_triggers_buy(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    # short: stop is ABOVE entry, target is BELOW entry
    await _seed_holding(repositories, quantity="-10", stop_loss="160", target_price="130")
    _mock_positions(qty="-10", current_price="165")  # above stop_loss=160
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None, side="buy")))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "165", side="buy")))
    _mock_fill_activities("act-1", "10", "165", side="buy")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    request_body = order_route.calls[0].request.content
    assert b'"side":"buy"' in request_body


@respx.mock
async def test_no_local_holding_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    _mock_positions(qty="10", current_price="100")  # would breach any threshold, but nothing on file
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.positions_checked == 1
    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_holding_with_no_thresholds_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, stop_loss=None, target_price=None)
    _mock_positions(qty="10", current_price="1")
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_breach_with_in_flight_intent_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # would breach stop_loss=140
    in_flight = TradeIntent(
        "ti-scan-1", "idem-scan-1", "corr-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "ai_scan", NOW,
        requested_quantity=Decimal("5"), status=TradeIntentStatus.ACCEPTED,
    )
    await repositories.trade_intents.create_once("ti-scan-1", in_flight, status=in_flight.status.value, unique_value=in_flight.idempotency_key)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_broker_outage_reports_degraded_status_and_alerts(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(side_effect=httpx.ConnectError("connection refused"))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "degraded"
    assert summary.error is not None
    assert summary.positions_checked == 0
    assert summary.exits_triggered == 0


@respx.mock
async def test_breach_is_skipped_when_symbol_execution_lock_already_held(tmp_path) -> None:
    """A concurrent scan already holds the per-symbol execution reservation
    for AAPL -- the monitor must skip this breach cleanly rather than racing
    to submit its own exit order."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # breaches stop_loss=140
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    database = repositories.trade_intents.database
    assert await reserve_symbol_for_execution(database, _aapl(), "another-coordinator") is True

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_monitor_stops_starting_new_work_when_lease_already_lost(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # would breach stop_loss=140
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    lease_lost = asyncio.Event()
    lease_lost.set()

    summary = await run_position_monitor(repositories, broker, gateway, alerts, clock=lambda: NOW, lease_lost=lease_lost)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0
