from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway
from tradepulse.models import ExecutionMode, ScanRunStatus, SessionState, TradingSession
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider, AnthropicAIProvider
from tradepulse.providers.anthropic_ai import SCAN_TOOL_NAME
from tradepulse.risk import save_session
from tradepulse.scanner import run_scan_cycle
from tradepulse.settlement import SettlementProcessor
from tradepulse.strategy import ExecutableUniverse


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")
UNIVERSE = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset())


def _tool_use_response(candidates: list[dict]) -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "tool_use", "name": SCAN_TOOL_NAME, "input": {"candidates": candidates}}],
    }


async def _setup(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    ai_provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    limits = risk_limits_for_profile("balanced")
    gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts, limits, ExecutionMode.PAPER, clock=lambda: NOW)
    return repositories, broker, ai_provider, market_data, gateway, limits


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
async def test_full_scan_cycle_executes_ai_recommended_buy(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "5", "199.60")))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1
    assert order_route.call_count == 1
    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_discovered == 1
    assert summary.candidates_approved == 1
    assert summary.orders_submitted == 1
    assert summary.execution_results[0].status == "filled"

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    assert scan_row["status"] == "completed"

    ai_rows = await repositories.ai_responses.list_all()
    assert len(ai_rows) == 1

    opp_rows = await repositories.opportunities.list_all()
    assert len(opp_rows) == 1
    assert hydrate("opportunities", opp_rows[0]["payload"]).asset.symbol == "AAPL"


@respx.mock
async def test_scan_cycle_skips_candidate_outside_executable_universe(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "ZZZZ", "recommendation": "BUY", "confidence": 90, "summary": "not in universe"}])
        )
    )
    _mock_account()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_discovered == 1
    assert summary.candidates_approved == 0
    assert summary.orders_submitted == 0
    assert order_route.call_count == 0


@respx.mock
async def test_scan_cycle_skips_entirely_when_session_is_kill_switched(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 0  # must never spend on an AI call while the kill switch is active
    assert summary.status == ScanRunStatus.FAILED
    assert summary.error == "SESSION_BLOCKED"


@respx.mock
async def test_scan_cycle_marks_failed_when_ai_provider_errors(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(429, json={"error": {"message": "rate limited"}}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.FAILED
    assert summary.error is not None
    assert (await repositories.ai_responses.list_all()) == []

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    assert scan_row["status"] == "failed"
