from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest, execution_lock_key
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Holding,
    SessionState,
    Side,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.risk import load_session, save_session
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


def _mock_account(cash: str = "50000", equity: str = "100000", last_equity: str = "99500") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": last_equity, "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_quote(bid: str = "199.50", ask: str = "199.60") -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
    )


def _mock_market_open(is_open: bool = True) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/clock").mock(
        return_value=httpx.Response(200, json={"is_open": is_open, "next_open": QUOTE_TS, "next_close": QUOTE_TS, "timestamp": QUOTE_TS})
    )


def _position_json(symbol: str = "AAPL", qty: str = "5", current_price: str = "150", avg_entry_price: str = "150", asset_class: str = "us_equity") -> dict:
    return {
        "symbol": symbol, "asset_class": asset_class, "qty": qty, "avg_entry_price": avg_entry_price,
        "market_value": "0", "current_price": current_price, "unrealized_pl": "0",
    }


def _mock_positions(*positions: dict) -> None:
    """Broker positions are the quantity/mark-price authority execute_intent
    now fetches on every call (see execution/gateway.py). Empty by default
    -- most tests don't hold anything locally or on the broker."""
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(200, json=list(positions))
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None) -> dict:
    return {
        "id": "order-1", "status": status, "symbol": "AAPL", "side": "buy",
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


def _fill_activity(
    activity_id: str, order_id: str = "order-1", *,
    symbol: str = "AAPL", side: str = "buy", qty: str = "5", price: str = "199.60", transaction_time: str = QUOTE_TS,
) -> dict:
    return {
        "id": activity_id, "activity_type": "FILL", "symbol": symbol, "side": side,
        "qty": qty, "price": price, "transaction_time": transaction_time, "order_id": order_id,
    }


def _mock_fill_activities(*activities: dict) -> respx.Route:
    return respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=list(activities))
    )


def _intent(trade_intent_id: str = "intent-1", *, broker_order_id: str = "order-1") -> TradeIntent:
    return TradeIntent(
        trade_intent_id=trade_intent_id, idempotency_key=trade_intent_id, correlation_id=trade_intent_id,
        asset=_aapl(), side=Side.BUY, execution_mode=ExecutionMode.PAPER, strategy="test", created_at=NOW,
        requested_quantity=Decimal("5"), status=TradeIntentStatus.ACCEPTED,
        broker_order_id=broker_order_id, client_order_id=trade_intent_id,
    )


@respx.mock
async def test_buy_rejected_when_session_not_active(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    _mock_positions()
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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")

    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] == "filled"

    holding_row = await repositories.holdings.get(asset_identity_key(_aapl()))
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("5")

    fill_rows = await repositories.fills.list_all(limit=10)
    assert len(fill_rows) == 1
    fill = hydrate("fills", fill_rows[0]["payload"])
    assert fill.fill_id == "act-1"  # the real Alpaca activity ID, not a synthesized order-id:fill:qty string
    assert fill.broker_fill_id == "act-1"


@respx.mock
async def test_buy_with_valid_symbol_reservation_proceeds_normally(tmp_path) -> None:
    """A caller participating in the reservation scheme, still holding its
    own lease, sees no behavior change -- the gateway's fence is a no-op
    when ownership genuinely still holds."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

    database = repositories.trade_intents.database
    owner_token = "owner-a"
    assert await acquire_lock(database, execution_lock_key(_aapl()), owner_token, "execute_intent", 45) is True

    request = ExecutionRequest(
        asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"),
        symbol_lock_owner_token=owner_token,
    )
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"


@respx.mock
async def test_buy_rejected_when_symbol_reservation_was_reclaimed_by_another_owner(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    database = repositories.trade_intents.database
    # A DIFFERENT owner holds the reservation -- as if it had been
    # legitimately reclaimed after this caller's lease expired.
    assert await acquire_lock(database, execution_lock_key(_aapl()), "owner-other", "execute_intent", 45) is True

    request = ExecutionRequest(
        asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"),
        symbol_lock_owner_token="owner-mine",
    )
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert result.reasons == ["EXECUTION_RESERVATION_LOST"]
    assert order_route.call_count == 0  # place_order never called -- no ambiguity to recover from


@respx.mock
async def test_buy_without_symbol_lock_owner_token_is_unaffected(tmp_path) -> None:
    """The default (None) means the caller isn't participating in the
    reservation scheme -- no new behavior, matching every pre-existing
    direct execute_intent test in this file."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    assert request.symbol_lock_owner_token is None
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"


@respx.mock
async def test_partial_fills_recorded_as_separate_activities_not_reconstructed_vwap(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.72")))
    _mock_fill_activities(
        _fill_activity("act-1", qty="2", price="199.50"),
        _fill_activity("act-2", qty="3", price="199.87"),
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")

    fill_rows = await repositories.fills.list_all(limit=10)
    fills = {row["record_id"]: hydrate("fills", row["payload"]) for row in fill_rows}
    assert set(fills) == {"act-1", "act-2"}
    assert fills["act-1"].quantity == Decimal("2") and fills["act-1"].price == Decimal("199.50")
    assert fills["act-2"].quantity == Decimal("3") and fills["act-2"].price == Decimal("199.87")


@respx.mock
async def test_activities_lag_behind_order_status_gateway_keeps_polling(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    gateway.FILL_TIMEOUT_SECONDS, gateway.POLL_INTERVAL_SECONDS = 5, 0.1
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[_fill_activity("act-1")])]
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")
    fill_rows = await repositories.fills.list_all(limit=10)
    assert len(fill_rows) == 1
    assert hydrate("fills", fill_rows[0]["payload"]).broker_fill_id == "act-1"


@respx.mock
async def test_foreign_order_activity_is_ignored(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(
        _fill_activity("act-1", order_id="order-1", qty="5"),
        _fill_activity("act-foreign", order_id="order-999", qty="3"),
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_quantity == Decimal("5")  # not 8 -- the foreign-order activity must never be attributed here
    fill_rows = await repositories.fills.list_all(limit=10)
    assert {row["record_id"] for row in fill_rows} == {"act-1"}


@respx.mock
async def test_attribute_order_fills_rejects_symbol_side_mismatch_and_alerts(tmp_path, caplog) -> None:
    """Unit-level: order_id matching alone is not trusted -- an activity
    linked to the right order but wrong symbol/side must be excluded and
    alerted, never turned into a Fill. Exercised directly against
    _attribute_order_fills rather than through the full 20s poll timeout,
    since every activity here is deliberately invalid for the whole window."""
    import logging

    repositories, broker, gateway = await _setup(tmp_path)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=[_fill_activity("act-bad", order_id="order-1", symbol="TSLA", qty="5")])
    )

    with caplog.at_level(logging.WARNING):
        attributed = await gateway._attribute_order_fills(_intent())
    await broker.aclose()

    assert attributed.quantity == Decimal("0")
    assert attributed.avg_price is None
    fill_rows = await repositories.fills.list_all(limit=10)
    assert fill_rows == []
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert any("BROKER_FILL_INTEGRITY_MISMATCH" in r.alert_message for r in skipped)


@respx.mock
async def test_attribute_order_fills_idempotent_on_repeated_calls(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=[_fill_activity("act-1", qty="5")])
    )

    intent = _intent()
    first = await gateway._attribute_order_fills(intent)
    second = await gateway._attribute_order_fills(intent)
    await broker.aclose()

    assert first.quantity == Decimal("5") and first.avg_price == Decimal("199.60")
    assert second.quantity == Decimal("5") and second.avg_price == Decimal("199.60")
    fill_rows = await repositories.fills.list_all(limit=10)
    assert len(fill_rows) == 1  # no duplicate created on the second, identical attribution pass


@respx.mock
async def test_attributed_quantity_exceeding_order_quantity_fails_closed(tmp_path, caplog) -> None:
    import logging

    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(
        _fill_activity("act-1", qty="5"),
        _fill_activity("act-2", qty="3"),  # a broker-side anomaly: activities for this order sum to more than order.filled_qty
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    with caplog.at_level(logging.WARNING):
        result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert result.reasons == ["BROKER_FILL_INTEGRITY_MISMATCH"]
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] not in ("filled", "rejected")  # never finalized as terminal on untrusted quantity
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert any("BROKER_FILL_INTEGRITY_MISMATCH" in r.alert_message for r in skipped)


@respx.mock
async def test_no_synthetic_fallback_when_no_activity_can_be_validated(tmp_path) -> None:
    """When Alpaca's Activities API never surfaces a validatable fill for
    the whole poll window, the gateway must leave the intent pending for
    reconciliation rather than fabricating a fill ID -- proving the old
    f"{order_id}:fill:{qty}" fallback path no longer exists."""
    repositories, broker, gateway = await _setup(tmp_path)
    gateway.FILL_TIMEOUT_SECONDS, gateway.POLL_INTERVAL_SECONDS = 1, 0.2
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-bad", symbol="TSLA", qty="5"))  # always invalid: wrong symbol

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert result.filled_quantity == Decimal("0")
    fill_rows = await repositories.fills.list_all(limit=10)
    assert fill_rows == []
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] == "accepted"  # never advanced to filled on unattributed/unvalidated quantity


@respx.mock
async def test_done_for_day_with_partial_fill_finalizes_as_partially_filled(tmp_path) -> None:
    """done_for_day is Alpaca's own distinct lifecycle status -- a genuine
    partial fill under it must land on PARTIALLY_FILLED, never be folded
    into FILLED just because it's a non-failure terminal order status."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("done_for_day", "3", "199.60")))
    _mock_fill_activities(_fill_activity("act-1", qty="3"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "partially_filled"
    assert result.filled_quantity == Decimal("3")
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] == "partially_filled"


@respx.mock
async def test_done_for_day_with_zero_fill_stays_non_terminal(tmp_path) -> None:
    """A done_for_day order with zero fills is inconclusive, not a
    cancellation -- Alpaca may still send updates the next trading day, so
    the intent must be left non-terminal rather than force-finalized."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("done_for_day", "0", None)))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert result.filled_quantity == Decimal("0")
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] not in ("filled", "canceled", "rejected", "expired")


@respx.mock
async def test_filled_with_partial_attributed_quantity_is_integrity_mismatch(tmp_path) -> None:
    """status=filled but /orders itself only reports partial coverage of
    what was requested -- distinct from the eventual-consistency lag case
    (which waits), this is Alpaca's own filled_qty contradicting what
    "filled" is supposed to mean, and must never finalize as FILLED."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "4", "199.60")))
    _mock_fill_activities(_fill_activity("act-1", qty="4"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert result.reasons == ["BROKER_ORDER_INTEGRITY_MISMATCH"]
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] != "filled"


@respx.mock
async def test_filled_avg_price_comes_from_attributed_activity_not_order_vwap(tmp_path) -> None:
    """order.filled_avg_price is Alpaca's VWAP across everything /orders
    currently reports filled -- it must never be paired with an attributed
    quantity/price that comes from a different, smaller set of validated
    activities. Here /orders reports a VWAP that doesn't match the single
    real activity at all; the persisted result must reflect the activity's
    own price, not the order-level figure."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    # Deliberately inconsistent VWAP vs. the real activity's own price below.
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "500.00")))
    _mock_fill_activities(_fill_activity("act-1", qty="5", price="199.60"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert result.filled_avg_price == Decimal("199.60")  # from the real activity, never the order's 500.00 VWAP

    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    intent = hydrate("trade_intents", intent_row["payload"])
    assert intent.filled_avg_price == Decimal("199.60")


@respx.mock
async def test_filled_status_with_zero_quantity_is_rejected_as_integrity_mismatch(tmp_path) -> None:
    """Alpaca reporting status=filled with filled_qty=0 is a broker-side
    contradiction -- must never finalize a TradeIntent as FILLED with no
    fill evidence at all."""
    repositories, broker, gateway = await _setup(tmp_path)
    gateway.FILL_TIMEOUT_SECONDS, gateway.POLL_INTERVAL_SECONDS = 1, 0.2
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "0", None)))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "pending"
    assert result.reasons == ["BROKER_ORDER_INTEGRITY_MISMATCH"]
    intent_row = await repositories.trade_intents.get(result.trade_intent_id)
    assert intent_row["status"] != "filled"


@respx.mock
async def test_terminal_intent_resumes_idempotently_without_resubmitting(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

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
    _mock_positions()
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
    """Cash below min_lot_notional -- even the soft cash-sizing cap can't
    produce anything executable, so this still rejects outright."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account(cash="0.50")  # nowhere near enough for 5 shares at ~$200, or even the $1 minimum lot
    _mock_positions()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in r for r in result.reasons)
    assert order_route.call_count == 0


@respx.mock
async def test_buy_with_partial_cash_sizes_down_instead_of_rejecting(tmp_path) -> None:
    """Enough cash for SOME shares, not the full request -- must size down,
    never reject outright (small-account support). Fill mocks dynamically
    echo whatever quantity the sizing math actually submits, rather than a
    hand-computed guess that could silently drift from the real formula."""
    import json as _json

    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account(cash="500")  # affords ~2.4 shares at ~$200 ask, not the full 5 requested
    _mock_positions()
    _mock_quote()
    _mock_market_open()

    state: dict[str, str] = {}

    def _accept(request: httpx.Request) -> httpx.Response:
        state["qty"] = _json.loads(request.content)["qty"]
        return httpx.Response(200, json=_order_json("accepted", "0", None))

    def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_json("filled", state["qty"], "199.60"))

    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept)
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(side_effect=_status)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        side_effect=lambda request: httpx.Response(200, json=[{
            "id": "act-1", "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
            "qty": state["qty"], "price": "199.60", "transaction_time": QUOTE_TS, "order_id": "order-1",
        }])
    )

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status != "rejected"
    assert Decimal("0") < result.filled_quantity < Decimal("5")


@respx.mock
async def test_buy_rejected_when_daily_loss_exceeds_limit(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    # balanced profile's max_daily_loss_pct is 1.0 -- a 1.5% decline must reject.
    _mock_account(equity="98500", last_equity="100000")
    _mock_positions()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("MAX_DAILY_LOSS_EXCEEDED" in r for r in result.reasons)
    assert order_route.call_count == 0

    session = await load_session(repositories)
    assert session.state == SessionState.RISK_STOPPED  # a genuine kill-switch condition latches the durable session, not just this one rejection
    assert session.kill_switch_reset_required is True
    events = await repositories.audit_events.list_all(limit=10)
    assert len(events) == 1


@respx.mock
async def test_ordinary_rejection_does_not_latch_risk_stop(tmp_path) -> None:
    """INSUFFICIENT_CASH is a per-trade sizing outcome, not an account-level
    kill-switch condition -- it must reject this one order without touching
    the durable session state. Cash below min_lot_notional ($1 default) so
    even the soft cash cap can't size this down to anything executable."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account(cash="0.50")
    _mock_positions()
    _mock_quote()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE
    assert (await repositories.audit_events.list_all(limit=10)) == []


@respx.mock
async def test_protective_exit_bypasses_only_the_session_gate_not_downstream_checks(tmp_path) -> None:
    """RISK_STOPPED must still allow a genuine protective exit through the
    session gate, but every downstream execution control keeps running --
    the exemption must not become a general safety bypass. Proven here by
    a broker account fetch failure (a check that happens AFTER the session
    gate) still correctly producing a skipped result."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    _mock_positions(_position_json(qty="5"))
    _mock_quote()
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(return_value=httpx.Response(500, json={"message": "internal error"}))
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.SELL, requested_quantity=Decimal("5"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "skipped"
    assert any("BROKER_UNAVAILABLE" in r for r in result.reasons)  # a downstream check, not the session gate, produced this rejection
    assert order_route.call_count == 0


def _btc() -> AssetIdentity:
    return AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")


@respx.mock
async def test_equity_buy_rejected_when_gateways_own_clock_check_says_closed(tmp_path) -> None:
    """The session itself still says ACTIVE (no scanner sync involved) --
    the gateway must independently re-verify live rather than trusting the
    session's label."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_market_open(is_open=False)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert result.reasons == ["EQUITY_MARKET_CLOSED"]
    assert order_route.call_count == 0

    session = await load_session(repositories)
    assert session.state == SessionState.ACTIVE  # the gateway's check is a pure guard -- it never writes to TradingSession


@respx.mock
async def test_equity_buy_rejected_when_clock_check_fails_with_transport_error(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    _mock_quote()
    respx.get("https://paper-api.alpaca.markets/v2/clock").mock(side_effect=httpx.ConnectError("connection refused"))
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("MARKET_CLOCK_UNAVAILABLE" in r for r in result.reasons)  # fail-closed, no crash
    assert order_route.call_count == 0


@respx.mock
async def test_crypto_buy_exempt_from_market_clock_check(tmp_path) -> None:
    """Crypto trades continuously -- no /v2/clock mock is registered at
    all; an unexpected call would fail this test via respx."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions()
    respx.get("https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes").mock(
        return_value=httpx.Response(200, json={"quotes": {"BTC/USD": {"bp": 60000.0, "ap": 60010.0, "t": QUOTE_TS}}})
    )
    order_json = {
        "id": "order-1", "status": "filled", "symbol": "BTC/USD", "side": "buy",
        "filled_qty": "0.01", "filled_avg_price": "60010", "submitted_at": QUOTE_TS,
    }
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=order_json))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=order_json))
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=[{
            "id": "act-btc-1", "activity_type": "FILL", "symbol": "BTC/USD", "side": "buy",
            "qty": "0.01", "price": "60010", "transaction_time": QUOTE_TS, "order_id": "order-1",
        }])
    )

    request = ExecutionRequest(asset=_btc(), side=Side.BUY, requested_quantity=Decimal("0.01"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"


@respx.mock
async def test_protective_exit_exempt_from_market_clock_check(tmp_path) -> None:
    """A stop-loss must still be able to fire even if the market technically
    shows closed -- the gateway's clock check must not pre-filter it; any
    real closure surfaces as an ordinary broker-level outcome instead."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_positions(_position_json(qty="5"))
    _mock_quote()
    _mock_market_open(is_open=False)
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "filled", "symbol": "AAPL", "side": "sell", "filled_qty": "5", "filled_avg_price": "199.60", "submitted_at": QUOTE_TS})
    )
    _mock_fill_activities(_fill_activity("act-1", side="sell", qty="5"))

    request = ExecutionRequest(asset=_aapl(), side=Side.SELL, requested_quantity=Decimal("5"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"  # never rejected EQUITY_MARKET_CLOSED despite the closed clock mock above


@respx.mock
async def test_protective_exit_uses_broker_quantity_not_understated_local_holding_even_when_integrity_blocked(tmp_path) -> None:
    """The exact accounting-drift scenario finding 1 fixes: local Holding
    understates the position (3) but Alpaca's real position is larger (10).
    A protective SELL for the full broker quantity must still classify as a
    protective exit -- and therefore must NOT be blocked by
    FINANCIAL_INTEGRITY_BLOCKED, the very state this drift would trigger."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession(
        "session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW,
        financial_integrity_reason="accounting drift", financial_integrity_manual_reenable_required=True,
    ))
    await repositories.holdings.create_once(
        asset_identity_key(_aapl()), Holding(asset=_aapl(), quantity=Decimal("3"), average_price=Decimal("150"), updated_at=NOW)
    )
    _mock_positions(_position_json(qty="10"))  # broker's real position is larger than the stale local Holding
    _mock_account()
    _mock_quote()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "filled", "symbol": "AAPL", "side": "sell", "filled_qty": "10", "filled_avg_price": "199.60", "submitted_at": QUOTE_TS})
    )
    _mock_fill_activities(_fill_activity("act-1", side="sell", qty="10"))

    request = ExecutionRequest(asset=_aapl(), side=Side.SELL, requested_quantity=Decimal("10"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"


@respx.mock
async def test_sell_not_covered_by_broker_quantity_is_not_protective_and_stays_blocked(tmp_path) -> None:
    """Inverse of the above -- a SELL that the broker's real position does
    NOT cover must not be misclassified as protective, and so still hits
    FINANCIAL_INTEGRITY_BLOCKED like any other new risk-taking action."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession(
        "session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW,
        financial_integrity_reason="accounting drift", financial_integrity_manual_reenable_required=True,
    ))
    _mock_positions(_position_json(qty="3"))  # broker position doesn't cover the requested exit
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.SELL, requested_quantity=Decimal("10"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert result.reasons == ["FINANCIAL_INTEGRITY_BLOCKED"]
    assert order_route.call_count == 0


@respx.mock
async def test_buy_exposure_uses_broker_current_price_not_local_cost_basis(tmp_path) -> None:
    """Finding 2: exposure gating must use the broker's current_price, not
    local cost basis. A held MSFT position's cost basis ($200 total) is
    negligible exposure (0.2%), but its broker current_price puts it at
    exactly the balanced profile's 40% max_total_exposure_pct ceiling
    ($40,000 of $100,000 equity) -- any further BUY must be rejected. Under
    the old cost-basis behavior this BUY would sail through instead."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    await repositories.holdings.create_once(
        "equity:default:alpaca:MSFT",
        Holding(asset=AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT"), quantity=Decimal("10"), average_price=Decimal("20"), updated_at=NOW),
    )
    _mock_positions(_position_json(symbol="MSFT", qty="10", current_price="4000"))  # $40,000 notional == 40% of equity
    _mock_account(cash="50000", equity="100000")
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "rejected"
    assert any("MAX_TOTAL_EXPOSURE_EXCEEDED" in r for r in result.reasons)
    assert order_route.call_count == 0


@respx.mock
async def test_protective_exit_resolves_correct_position_when_broker_reports_same_symbol_across_asset_classes(tmp_path) -> None:
    """Finding 1: two broker positions share display-symbol text ('AAPL')
    but different asset classes -- held_quantity/mark_prices for an equity
    AAPL request must resolve to the EQUITY position, not whichever entry a
    bare-symbol match or dict-overwrite would have picked."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_positions(
        _position_json(symbol="AAPL", qty="5", current_price="150", asset_class="us_equity"),
        _position_json(symbol="AAPL", qty="2", current_price="60000", asset_class="crypto"),
    )
    _mock_account()
    _mock_quote()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "filled", "symbol": "AAPL", "side": "sell", "filled_qty": "5", "filled_avg_price": "150", "submitted_at": QUOTE_TS})
    )
    _mock_fill_activities(_fill_activity("act-1", side="sell", qty="5", price="150"))

    # Selling the EQUITY quantity (5) -- if the gateway mismatched onto the
    # crypto position's held_quantity (2), this would fail as
    # INSUFFICIENT_POSITION_TO_SELL or reject as non-protective.
    request = ExecutionRequest(asset=_aapl(), side=Side.SELL, requested_quantity=Decimal("5"), strategy="test")
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"


@respx.mock
async def test_execute_intent_fails_closed_on_duplicate_canonical_broker_position_key(tmp_path) -> None:
    """A malformed/duplicate broker response -- two positions both mapping
    to the same canonical asset key -- must never be silently resolved by
    picking whichever a dict comprehension kept last. Gateway refuses to
    guess and never reaches order placement."""
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_positions(
        _position_json(symbol="AAPL", qty="5", current_price="150"),
        _position_json(symbol="AAPL", qty="9", current_price="999"),
    )
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "skipped"
    assert any("DUPLICATE_BROKER_POSITION_KEY" in r for r in result.reasons)
    assert order_route.call_count == 0


@respx.mock
async def test_buy_approved_when_daily_loss_under_limit(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    # A 0.5% decline is under the balanced profile's 1.0% limit -- must NOT reject on this basis.
    _mock_account(equity="99500", last_equity="100000")
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "1", "199.60")))
    _mock_fill_activities(_fill_activity("act-1", qty="1"))

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("1"), strategy="test", confidence=Decimal("90"))
    result = await gateway.execute_intent(request)
    await broker.aclose()

    assert result.status == "filled"
    assert order_route.call_count == 1


@respx.mock
async def test_kill_switch_active_rejects_buy_even_with_everything_else_valid(tmp_path) -> None:
    repositories, broker, gateway = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    _mock_positions()
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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=httpx.ConnectError("connection refused"))
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None))
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(500, json={"message": "internal error"})
    )
    lookup_route = respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None))
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities(_fill_activity("act-1"))

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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
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
    _mock_positions()
    _mock_quote()
    _mock_market_open()
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
