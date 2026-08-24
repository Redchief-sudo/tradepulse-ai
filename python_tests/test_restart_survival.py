"""Proves the full chain -- broker submission, fill polling, settlement,
lot/holding/PnL projection, the scan lock -- survives a crash at each
realistic boundary and resumes into the same correct state.

Every "restart" is simulated the same way: construct a genuinely SECOND,
independent set of AlpacaClient/AlpacaMarketDataProvider/ExecutionGateway/
SettlementProcessor objects pointed at the SAME SQLite file (a second
AsyncSQLiteDatabase against the same path), never reusing the first
process's Python objects. That's what distinguishes "resumed via persisted
state" from "the same in-memory objects happened to be called twice" --
matching this system's real invocation model (a fresh `tradepulse` process
per cron firing, reading whatever the last process left in the database).
"""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.cli import SCAN_LOCK_KEY, _run_scan_leg
from tradepulse.config import Settings, risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    ReconciliationOutcome,
    SessionState,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, hydrate
from tradepulse.providers import AlpacaMarketDataProvider, AnthropicAIProvider
from tradepulse.providers.anthropic_ai import SCAN_TOOL_NAME
from tradepulse.reconciliation import run_reconciliation
from tradepulse.risk import save_session
from tradepulse.scanner import run_scan_cycle
from tradepulse.settlement import SettlementProcessor
from tradepulse.strategy import ExecutableUniverse

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _fresh_gateway(db_url: str):
    """Builds a genuinely independent composition root against `db_url` --
    call this once per simulated "process" in a test, never reusing the
    returned objects across a restart boundary."""
    database = AsyncSQLiteDatabase(db_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    limits = risk_limits_for_profile("balanced")
    gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts, limits, ExecutionMode.PAPER, clock=lambda: NOW)
    return database, repositories, broker, market_data, gateway, settlement, limits


def _mock_account(cash: str = "50000", equity: str = "100000") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": "99500", "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_quote(bid: str = "199.50", ask: str = "199.60") -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None, order_id: str = "order-1", side: str = "buy") -> dict:
    return {
        "id": order_id, "status": status, "symbol": "AAPL", "side": side,
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


def _mock_fill_activities(activity_id: str, qty: str, price: str, order_id: str = "order-1", side: str = "buy") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": side,
                "qty": qty, "price": price, "transaction_time": QUOTE_TS, "order_id": order_id,
            }],
        )
    )


def _tool_use_response(candidates: list[dict]) -> dict:
    return {
        "model": "claude-haiku-4-5",
        "content": [{"type": "tool_use", "name": SCAN_TOOL_NAME, "input": {"candidates": candidates}}],
    }


def _synthetic_closes(n: int, trend: float, amplitude: float, period: float, phase: float) -> list[float]:
    price = 100.0
    closes = []
    for i in range(n):
        price += trend + amplitude * math.sin(i / period + phase)
        closes.append(price)
    return closes


def _bars_json(closes: list[float]) -> dict:
    """Daily bars, most-recent-first (matches the `sort=desc` the client
    requests) -- oldest day first in `closes`, so build newest-first here."""
    end = NOW
    rows = []
    for offset, close in enumerate(closes):
        day = end - timedelta(days=len(closes) - 1 - offset)
        rows.append({
            "t": day.isoformat().replace("+00:00", "Z"), "o": close * 0.998, "h": close * 1.006,
            "l": close * 0.994, "c": close, "v": 1_000_000.0,
        })
    return {"bars": list(reversed(rows))}  # newest-first


# Empirically verified (see test_scanner_coordinator.py) to produce a BUY
# deterministic signal with the default strategy weights.
_BULLISH_CLOSES = _synthetic_closes(40, trend=0.4, amplitude=2.0, period=4.0, phase=0.5)


def _mock_bars(closes: list[float]) -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(closes))
    )


@respx.mock
async def test_resume_after_crash_before_broker_submission(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path}/test.db"

    # Process A: crashed the instant after persisting RISK_APPROVED, before
    # ever calling broker.place_order -- simulate by seeding exactly that
    # row directly, matching what execute_intent would have written.
    _, repositories_a, _, _, _, _, _ = await _fresh_gateway(db_url)
    await save_session(repositories_a, TradingSession("session", SessionState.ACTIVE, True, NOW))
    idempotency_key = "ik-test-decision-1-AAPL-buy"  # derive_idempotency_key(strategy, decision_id, None, symbol, side)
    crashed_intent = TradeIntent(
        "ti-1", idempotency_key, "decision-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("5"), reference_price=Decimal("199.55"), status=TradeIntentStatus.RISK_APPROVED,
    )
    await repositories_a.trade_intents.create_once("ti-1", crashed_intent, status=crashed_intent.status.value, unique_value=idempotency_key)

    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities("act-1", "5", "199.60")

    # Process B: fresh gateway, same database file.
    _, _, broker_b, _, gateway_b, _, _ = await _fresh_gateway(db_url)
    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", decision_id="decision-1")
    result = await gateway_b.execute_intent(request)
    await broker_b.aclose()

    assert order_route.call_count == 1  # never double-submitted
    assert result.trade_intent_id == "ti-1"  # the crashed intent's id was reused, not a new one
    assert result.status == "filled"


@respx.mock
async def test_resume_after_crash_between_acceptance_and_fill_polling(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path}/test.db"

    # Process A: crashed right after the broker accepted the order (broker_order_id
    # recorded) but before _poll_and_settle ever ran.
    _, repositories_a, _, _, _, _, _ = await _fresh_gateway(db_url)
    await save_session(repositories_a, TradingSession("session", SessionState.ACTIVE, True, NOW))
    idempotency_key = "ik-test-decision-2-AAPL-buy"
    crashed_intent = TradeIntent(
        "ti-2", idempotency_key, "decision-2", _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("5"), reference_price=Decimal("199.55"), status=TradeIntentStatus.ACCEPTED,
        broker_order_id="order-1", client_order_id="ti-2",
    )
    await repositories_a.trade_intents.create_once("ti-2", crashed_intent, status=crashed_intent.status.value, unique_value=idempotency_key)

    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities("act-2", "5", "199.60")

    # Process B: fresh gateway, same database file, resumes polling the SAME broker order.
    _, repositories_b, broker_b, _, gateway_b, _, _ = await _fresh_gateway(db_url)
    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", decision_id="decision-2")
    result = await gateway_b.execute_intent(request)
    await broker_b.aclose()

    assert order_route.call_count == 0  # never resubmitted -- resumed polling the existing order
    assert result.trade_intent_id == "ti-2"
    assert result.status == "filled"

    holding_row = await repositories_b.holdings.get("AAPL")
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("5")


@respx.mock
async def test_resume_after_crash_mid_settlement_stale_processing(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path}/test.db"

    # Process A: a real buy, fully accepted and filled through the gateway,
    # producing a genuine Fill + SettlementEvent.
    _, repositories_a, broker_a, _, gateway_a, _, _ = await _fresh_gateway(db_url)
    await save_session(repositories_a, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_account()
    _mock_quote()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))
    _mock_fill_activities("act-3", "5", "199.60")

    request = ExecutionRequest(asset=_aapl(), side=Side.BUY, requested_quantity=Decimal("5"), strategy="test", decision_id="decision-3")
    first_result = await gateway_a.execute_intent(request)
    await broker_a.aclose()
    assert first_result.status == "filled"

    # Simulate the crash: settlement had claimed the event (PROCESSING) but
    # never got to COMPLETED, and its lease is now stale.
    settlement_rows = await repositories_a.settlements.list_all()
    assert len(settlement_rows) == 1
    event = hydrate("settlements", settlement_rows[0]["payload"])
    stale = replace(event, status=SettlementStatus.PROCESSING, processing_owner="dead-worker", processing_started_at=NOW - timedelta(seconds=300))
    await repositories_a.settlements.update(event.settlement_event_id, stale, status=stale.status.value)

    # Process B: fresh settlement processor, same database file.
    _, repositories_b, _, _, _, settlement_b, _ = await _fresh_gateway(db_url)
    summary = await settlement_b.process_pending(stale_lease_seconds=120)
    assert summary.completed == 1

    holding_row = await repositories_b.holdings.get("AAPL")
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("5")
    assert holding.average_price == Decimal("199.60")

    lot_rows = await repositories_b.position_lots.list_all()
    assert len(lot_rows) == 1  # not duplicated by the replay
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.remaining_quantity == Decimal("5")

    intent_row = await repositories_b.trade_intents.get(first_result.trade_intent_id)
    intent = hydrate("trade_intents", intent_row["payload"])
    assert intent.realized_pnl in (None, Decimal("0"))  # a fresh long entry realizes nothing yet, and it's not doubled


@respx.mock
async def test_stale_scan_lock_is_reclaimed_after_a_crash(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path}/test.db"
    database, repositories, broker, market_data, gateway, _, _ = await _fresh_gateway(db_url)

    # Process A acquired the scan lock and crashed without releasing it --
    # simulate by pre-acquiring with an already-expired TTL.
    assert await acquire_lock(database, SCAN_LOCK_KEY, "dead-process", "scan", ttl_seconds=-1) is True

    # A blocked session keeps this fast and network-free -- this test is
    # about the lock, not the scan cycle's own logic.
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    settings = Settings.from_env({
        "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key",
        "TRADEPULSE_DATABASE_URL": db_url,
    })
    ai_provider = AnthropicAIProvider("key", "claude-haiku-4-5", 10)

    # Process B: a fresh scan invocation must reclaim the stale lease, not honor it.
    result = await _run_scan_leg(database, repositories, ai_provider, market_data, broker, gateway, settings)
    await broker.aclose()
    await ai_provider.aclose()

    assert result is not None  # None would mean it saw the lock as still held and skipped
    assert result.error == "SESSION_BLOCKED"


@respx.mock
async def test_full_story_scan_opens_monitor_protects_reconcile_confirms_clean(tmp_path) -> None:
    """The complete chain the audit asked about, threaded through three
    independent simulated process restarts: scan opens a position -> restart
    -> monitor protectively closes it on a stop breach -> restart ->
    reconciliation finds broker/lots/holding/fills all agree, zero drift."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    universe = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset())

    _mock_account()
    _mock_quote()

    # The buy order's accept response never carries quantity, but the
    # subsequent GET /orders/order-1 and activities polls must reflect
    # whatever quantity the scanner's real notional/price sizing actually
    # computed and submitted -- a fully filled order's filled_qty must
    # exactly equal what it was submitted with (see fill_attribution.py's
    # terminal_status_for_order), so a hardcoded guess here would silently
    # drift from the real request.
    placed: dict[str, str] = {}
    post_call_count = {"n": 0}

    def _accept_order(request: httpx.Request) -> httpx.Response:
        post_call_count["n"] += 1
        if post_call_count["n"] == 1:
            placed["buy_qty"] = json.loads(request.content)["qty"]
            return httpx.Response(200, json=_order_json("accepted", "0", None, order_id="order-1", side="buy"))
        return httpx.Response(200, json=_order_json("accepted", "0", None, order_id="order-2", side="sell"))

    orders_post_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept_order)

    def _order1_status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_json("filled", placed["buy_qty"], "199.60", order_id="order-1", side="buy"))

    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(side_effect=_order1_status)

    def _activities_a(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{
                "id": "activity-buy-1", "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
                "qty": placed["buy_qty"], "price": "199.60", "transaction_time": QUOTE_TS, "order_id": "order-1",
            }],
        )

    activities_route = respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(side_effect=_activities_a)

    # ---- Process A: scan discovers AAPL and opens a long position. ----
    _, repositories_a, broker_a, market_data_a, gateway_a, _, limits_a = await _fresh_gateway(db_url)
    ai_provider_a = AnthropicAIProvider("key", "claude-haiku-4-5", 10)
    await save_session(repositories_a, TradingSession("session", SessionState.ACTIVE, True, NOW))
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_bars(_BULLISH_CLOSES)

    scan_summary = await run_scan_cycle(repositories_a, ai_provider_a, market_data_a, broker_a, gateway_a, universe, limits_a, clock=lambda: NOW)
    await broker_a.aclose()
    await ai_provider_a.aclose()

    assert scan_summary.orders_submitted == 1
    holding_row = await repositories_a.holdings.get("AAPL")
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    bought_qty = holding.quantity
    stop_loss = holding.stop_loss
    assert stop_loss is not None  # the fix from this same round -- the scanner now sets one

    # ---- Restart. Process B: the monitor sees the stop breached, exits. ----
    _, repositories_b, broker_b, _, gateway_b, _, _ = await _fresh_gateway(db_url)
    alerts_b = TelegramAlerter(None, None)

    breach_price = str(stop_loss - Decimal("10"))
    positions_route = respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(200, json=[{
            "symbol": "AAPL", "asset_class": "us_equity", "qty": str(bought_qty), "avg_entry_price": "199.60",
            "market_value": "0", "current_price": breach_price, "unrealized_pl": "0",
        }])
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-2").mock(
        return_value=httpx.Response(200, json=_order_json("filled", str(bought_qty), breach_price, order_id="order-2", side="sell"))
    )
    activities_route.mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": "activity-sell-1", "activity_type": "FILL", "symbol": "AAPL", "side": "sell",
                "qty": str(bought_qty), "price": breach_price, "transaction_time": QUOTE_TS, "order_id": "order-2",
            }],
        )
    )

    monitor_summary = await run_position_monitor(repositories_b, broker_b, gateway_b, alerts_b, clock=lambda: NOW)
    await broker_b.aclose()

    assert monitor_summary.status == "ok"
    assert monitor_summary.exits_triggered == 1
    assert orders_post_route.call_count == 2  # one buy, one protective sell -- never more
    assert await repositories_b.holdings.get("AAPL") is None  # position fully closed

    # ---- Restart. Process C: reconciliation confirms clean state. ----
    _, repositories_c, broker_c, _, _, settlement_c, _ = await _fresh_gateway(db_url)
    alerts_c = TelegramAlerter(None, None)

    positions_route.mock(return_value=httpx.Response(200, json=[]))  # broker shows no position, matching the closed local state
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(200, json=[
            {"id": "activity-buy-1", "activity_type": "FILL", "symbol": "AAPL", "side": "buy", "qty": str(bought_qty), "price": "199.60", "transaction_time": QUOTE_TS},
            {"id": "activity-sell-1", "activity_type": "FILL", "symbol": "AAPL", "side": "sell", "qty": str(bought_qty), "price": breach_price, "transaction_time": QUOTE_TS},
        ])
    )

    reconcile_summary = await run_reconciliation(repositories_c, broker_c, settlement_c, alerts_c, clock=lambda: NOW)
    await broker_c.aclose()

    assert reconcile_summary.status == "ok"
    assert reconcile_summary.accounting_drift_detected == 0
    assert reconcile_summary.missed_fills_detected == 0

    # No position record at all: with the position fully closed everywhere
    # (broker, lots, and holding all show zero exposure), there's nothing to
    # reconcile for that symbol -- only the two fill matches are expected.
    records = await repositories_c.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    assert len(payloads) == 2
    assert all(p.outcome == ReconciliationOutcome.MATCHED for p in payloads)
    assert all(p.reconciliation_type == "fill" for p in payloads)
