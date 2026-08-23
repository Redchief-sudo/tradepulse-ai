from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, SessionState, Side, TradingSession
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.risk import save_session
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
    return repositories, broker, gateway


def _mock_account(cash: str = "50000", equity: str = "100000") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": "99500", "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_quote(bid: str = "199.50", ask: str = "199.60") -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None) -> dict:
    return {
        "id": "order-1", "status": status, "symbol": "AAPL", "side": "buy",
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


@respx.mock
async def test_buy_rejected_when_session_not_active(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()
    assert result.status == "rejected"
    assert "TRADING_SESSION_NOT_ACTIVE" in result.reasons[0]


@respx.mock
async def test_full_buy_flow_fills_and_settles(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")

    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] == "filled"

    holding_row = await repositories.holdings.get("AAPL")
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("5")


@respx.mock
async def test_terminal_intent_resumes_idempotently_without_resubmitting(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", decision_id="decision-1")
    first = await gateway.execute_intent(request)
    second = await gateway.execute_intent(request)
    await broker.aclose()

    assert first.trade_intent_id == second.trade_intent_id
    assert second.status == "filled"
    assert order_route.call_count == 1  # not resubmitted on the idempotent replay


@respx.mock
async def test_buy_rejected_when_confidence_below_minimum_and_never_reaches_broker(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test", confidence=Decimal("10"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("CONFIDENCE_BELOW_MIN" in r for r in result.reasons)
    assert order_route.call_count == 0


@respx.mock
async def test_buy_rejected_when_broker_cash_insufficient(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account(cash="10")  # nowhere near enough for 5 shares at ~$200
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("INSUFFICIENT_CASH" in r for r in result.reasons)
    assert order_route.call_count == 0


@respx.mock
async def test_kill_switch_active_rejects_buy_even_with_everything_else_valid(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert result.reasons == ["KILL_SWITCH_ACTIVE"]
    assert order_route.call_count == 0


@respx.mock
async def test_ambiguous_submission_error_recovers_via_client_order_id_lookup(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=httpx.ConnectError("connection refused"))
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None))
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")
    assert order_route.call_count == 1  # never resubmitted after the connection error
    assert lookup_route.call_count == 1


@respx.mock
async def test_definitive_rejection_ends_rejected_without_recovery_lookup(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(422, json={"message": "invalid order", "code": 40010001})
    )
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None))
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert order_route.call_count == 1
    assert lookup_route.call_count == 0  # a definitive rejection must never trigger a recovery lookup


@respx.mock
async def test_ambiguous_5xx_error_routes_through_recovery_not_straight_to_rejected(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None))
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"  # proves it went through recovery, not straight to "rejected"
    assert order_route.call_count == 1
    assert lookup_route.call_count == 1


@respx.mock
async def test_recovery_lookup_404_ends_submission_unknown_and_never_resubmits(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=httpx.ConnectError("connection refused"))
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(return_value=httpx.Response(404))

    request = ExecutionRequest(
        asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test",
        decision_id="decision-unknown", confidence=Decimal("90"),
    )
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert order_route.call_count == 1
    assert lookup_route.call_count == 1

    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] == "submission_unknown"


@respx.mock
async def test_second_call_while_submission_unknown_retries_recovery_without_resubmitting(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=httpx.ConnectError("connection refused"))
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(return_value=httpx.Response(404))

    request = ExecutionRequest(
        asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test",
        decision_id="decision-retry", confidence=Decimal("90"),
    )
    first = await gateway.execute_intent(request)
    second = await gateway.execute_intent(request)
    await broker.aclose()

    assert first.status == "pending"
    assert second.status == "pending"
    assert first.trade_intent_id == second.trade_intent_id
    assert order_route.call_count == 1  # never resubmitted while the outcome is unresolved
    assert lookup_route.call_count == 2  # recovery re-attempted on the second call

    intent_row = await repositories.trade_intents.get(first.trade_intent_id)
    assert intent_row["status"] == "submission_unknown"
