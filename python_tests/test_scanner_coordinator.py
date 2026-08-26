import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, reserve_symbol_for_execution
from tradepulse.models import AssetClass, AssetIdentity, Candle, ExecutionMode, ScanRun, ScanRunStatus, ScanTrigger, SessionState, TradingSession, asset_identity_key
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider, AnthropicAIProvider
from tradepulse.providers.anthropic_ai import SCAN_TOOL_NAME
from tradepulse.risk import load_session, save_session
from tradepulse.scanner import run_scan_cycle
from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price
from tradepulse.settlement import SettlementProcessor
from tradepulse.strategy import ExecutableUniverse, atr

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")
UNIVERSE = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset())


def _tool_use_response(candidates: list[dict]) -> dict:
    return {
        "model": "claude-haiku-4-5",
        "content": [{"type": "tool_use", "name": SCAN_TOOL_NAME, "input": {"candidates": candidates}}],
    }


async def _setup(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    ai_provider = AnthropicAIProvider("key", "claude-haiku-4-5", 10)
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


def _mock_positions(*positions: dict) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=list(positions)))


def _mock_market_open(is_open: bool = True) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/clock").mock(
        return_value=httpx.Response(200, json={"is_open": is_open, "next_open": QUOTE_TS, "next_close": QUOTE_TS, "timestamp": QUOTE_TS})
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None) -> dict:
    return {
        "id": "order-1", "status": status, "symbol": "AAPL", "side": "buy",
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


def _mock_dynamic_full_fill(price: str = "199.60", activity_id: str = "act-1") -> respx.Route:
    """Wires order placement -> status polling -> fill activity together so
    the mocked fill always reflects whatever quantity the scanner's real
    notional/price position sizing actually computed and submitted, instead
    of a hardcoded guess that could silently drift from it (as it did once
    the terminal-status mapping became quantity-aware -- a fully filled
    order's filled_qty must exactly equal what it was submitted with)."""
    import json as _json

    state: dict[str, str] = {}

    def _accept(request: httpx.Request) -> httpx.Response:
        state["qty"] = _json.loads(request.content)["qty"]
        return httpx.Response(200, json=_order_json("accepted", "0", None))

    def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_json("filled", state["qty"], price))

    def _activities(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{
                "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
                "qty": state["qty"], "price": price, "transaction_time": QUOTE_TS, "order_id": "order-1",
            }],
        )

    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept)
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(side_effect=_status)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(side_effect=_activities)
    return order_route


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


# Empirically verified (via strategy.compute_real_factors/weighted_composite
# with the default strategy weights) to produce a BUY deterministic signal.
_BULLISH_CLOSES = _synthetic_closes(40, trend=0.4, amplitude=2.0, period=4.0, phase=0.5)
# A choppy, mildly declining series -- deterministic signal lands in
# HOLD/SELL/STRONG_SELL, never BUY/STRONG_BUY.
_BEARISH_CLOSES = _synthetic_closes(40, trend=-0.3, amplitude=3.0, period=2.0, phase=0.0)


def _mock_bars(closes: list[float]) -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(closes))
    )


@respx.mock
async def test_full_scan_cycle_executes_ai_recommended_buy(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    order_route = _mock_dynamic_full_fill()

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
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.asset.symbol == "AAPL"
    assert opportunity.source == "anthropic"  # reflects the actual AI backend used, not a hardcoded string

    # A protective stop must actually be set -- ATR-based (the scanner's
    # primary source; see scanner/coordinator.py::_atr_stop_loss_price)
    # against the scanner's own quote (mid of the mocked 199.50/199.60
    # bid/ask) and the same synthetic candle series used to discover this
    # candidate, since the AI/composite never supply one; without this the
    # position monitor has nothing to protect. Computed via the actual atr()
    # function rather than hand-derived so this doesn't silently drift out
    # of sync with the implementation.
    holding_row = await repositories.holdings.get(asset_identity_key(AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")))
    holding = hydrate("holdings", holding_row["payload"])
    highs = [c * 1.006 for c in _BULLISH_CLOSES]
    lows = [c * 0.994 for c in _BULLISH_CLOSES]
    atr_value = atr(highs, lows, _BULLISH_CLOSES)
    expected_distance = Decimal(str(atr_value)) * limits.atr_stop_multiplier
    expected_stop = (Decimal("199.55") - expected_distance).quantize(Decimal("0.01"))
    assert holding.stop_loss == expected_stop

    # A broker-truth equity snapshot must be persisted every cycle -- otherwise
    # check_max_drawdown() has no history to compare against and can never trip.
    snapshot_rows = await repositories.equity_snapshots.list_all()
    assert len(snapshot_rows) == 1
    snapshot = hydrate("equity_snapshots", snapshot_rows[0]["payload"])
    assert snapshot.total_equity == Decimal("100000")
    assert snapshot.source == "broker"


@respx.mock
async def test_deterministic_gate_rejects_ai_buy_when_composite_disagrees(tmp_path, caplog) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "AI likes it."}])
        )
    )
    _mock_account()
    _mock_quote()
    _mock_bars(_BEARISH_CLOSES)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_discovered == 1
    assert summary.candidates_approved == 0  # AI said BUY, but the deterministic composite disagreed
    assert order_route.call_count == 0
    assert (await repositories.opportunities.list_all()) == []

    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "DETERMINISTIC_SIGNAL_DISAGREED"
    assert rejected[0].ai_recommendation == "BUY"


@respx.mock
async def test_deterministic_gate_fails_closed_on_insufficient_candle_history(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "AI likes it."}])
        )
    )
    _mock_account()
    _mock_quote()
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": []})  # fewer than MIN_CANDLES
    )
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 0
    assert order_route.call_count == 0


@respx.mock
async def test_scan_cycle_skips_candidate_outside_executable_universe(tmp_path, caplog) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "ZZZZ", "recommendation": "BUY", "confidence": 90, "summary": "not in universe"}])
        )
    )
    _mock_account()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_discovered == 1
    assert summary.candidates_approved == 0
    assert summary.orders_submitted == 0
    assert order_route.call_count == 0

    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].symbol == "ZZZZ"
    assert rejected[0].reason == "OUTSIDE_EXECUTABLE_UNIVERSE"


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
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(429, json={"error": {"message": "rate limited"}}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.FAILED
    assert summary.error is not None
    assert (await repositories.ai_responses.list_all()) == []

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    assert scan_row["status"] == "failed"


@respx.mock
async def test_stale_running_scan_run_is_reclaimed_as_failed(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    stale = ScanRun(
        scan_run_id="stale-1", scan_generation="gen-stale", trigger=ScanTrigger.SCHEDULED,
        status=ScanRunStatus.RUNNING, started_at=NOW - timedelta(seconds=1000), lock_owner_token="owner-1",
    )
    await repositories.scan_runs.create_once("stale-1", stale, status=stale.status.value)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    stale_row = await repositories.scan_runs.get("stale-1")
    assert stale_row["status"] == "failed"
    stale_after = hydrate("scan_runs", stale_row["payload"])
    assert stale_after.error == "CRASHED_STALE_SCAN_RUN"
    assert stale_after.completed_at == NOW


@respx.mock
async def test_recent_running_scan_run_is_left_alone(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    recent = ScanRun(
        scan_run_id="recent-1", scan_generation="gen-recent", trigger=ScanTrigger.SCHEDULED,
        status=ScanRunStatus.RUNNING, started_at=NOW - timedelta(seconds=5), lock_owner_token="owner-1",
    )
    await repositories.scan_runs.create_once("recent-1", recent, status=recent.status.value)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    recent_row = await repositories.scan_runs.get("recent-1")
    assert recent_row["status"] == "running"


@respx.mock
async def test_scan_cycle_syncs_session_to_market_closed_before_evaluating_candidates(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open(is_open=False)
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1  # equity discovery still runs -- the sync doesn't hard-block the cycle, only new equity exposure
    session = await load_session(repositories)
    assert session.state == SessionState.MARKET_CLOSED


@respx.mock
async def test_scan_cycle_never_checks_market_clock_when_session_is_disabled(tmp_path) -> None:
    """No session row at all -- defaults to DISABLED, which isn't ACTIVE or
    MARKET_CLOSED, so sync_market_session is never even called (DISABLED
    doesn't hard-block AI discovery itself -- only RISK_STOPPED/
    FINANCIAL_INTEGRITY_BLOCKED do; actual execution authority is enforced
    later at the gateway). Proven via respx: no /v2/clock mock is
    registered, so an unexpected call fails the test."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1  # proceeds past the AI call -- never blocked at the SESSION_BLOCKED gate
    assert summary.error != "SESSION_BLOCKED"


@respx.mock
async def test_scan_cycle_rejects_candidate_when_symbol_execution_lock_already_held(tmp_path, caplog) -> None:
    """A concurrent monitor (or a second scan) already holds the per-symbol
    execution reservation for AAPL -- this scan cycle must skip it cleanly
    rather than racing to submit, and never place an order."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    database = repositories.trade_intents.database
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    assert await reserve_symbol_for_execution(database, asset, "another-coordinator") is True

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 0
    assert order_route.call_count == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "SYMBOL_EXECUTION_LOCKED"


@respx.mock
async def test_scan_cycle_stops_starting_new_work_when_lease_already_lost(tmp_path, caplog) -> None:
    """A command whose own lease may no longer be exclusive must stop
    starting new work -- discovery still runs (AI is still called), but no
    candidate reaches order placement."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    lease_lost = asyncio.Event()
    lease_lost.set()  # already lost before the loop even starts

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(
            repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, clock=lambda: NOW, lease_lost=lease_lost,
        )
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1  # discovery still ran
    assert summary.candidates_approved == 0
    assert order_route.call_count == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "COMMAND_LEASE_LOST"


def _synthetic_candles(closes: list[float], band: float = 0.006) -> list[Candle]:
    end = NOW
    return [
        Candle(
            date=(end - timedelta(days=len(closes) - 1 - i)).date().isoformat(),
            open=Decimal(str(close * (1 - band / 3))), high=Decimal(str(close * (1 + band))),
            low=Decimal(str(close * (1 - band))), close=Decimal(str(close)), volume=Decimal("1000000"),
        )
        for i, close in enumerate(closes)
    ]


def test_atr_stop_loss_used_when_valid_and_within_sanity_band() -> None:
    candles = _synthetic_candles(_BULLISH_CLOSES)
    price = Decimal("199.55")
    result = _atr_stop_loss_price(price, candles, Decimal("2"), AssetClass.EQUITY, Decimal("0.5"), Decimal("25"))
    assert result is not None
    assert result < price  # a real protective distance below the reference price

    highs = [c * 1.006 for c in _BULLISH_CLOSES]
    lows = [c * 0.994 for c in _BULLISH_CLOSES]
    expected = (price - Decimal(str(atr(highs, lows, _BULLISH_CLOSES))) * Decimal("2")).quantize(Decimal("0.01"))
    assert result == expected


def test_atr_stop_loss_falls_back_to_fixed_pct_when_atr_unavailable() -> None:
    """Too little candle history for atr()'s own minimum -- falls back to
    the fixed-pct stop, never crashes or produces a nonsensical stop."""
    too_few_candles = _synthetic_candles(_BULLISH_CLOSES[:5])
    price = Decimal("199.55")
    assert _atr_stop_loss_price(price, too_few_candles, Decimal("2"), AssetClass.EQUITY, Decimal("0.5"), Decimal("25")) is None

    fallback = _stop_loss_price(price, Decimal("8"), AssetClass.EQUITY)
    assert fallback == (price * Decimal("0.92")).quantize(Decimal("0.01"))


def test_atr_stop_loss_falls_back_when_distance_is_pathologically_small() -> None:
    """A near-flat candle series produces a near-zero ATR -- the resulting
    distance would be pathologically close to price (a tiny risk_per_share
    denominator feeding an absurdly large risk-based quantity). Must fall
    back to the fixed-pct stop instead of accepting it."""
    flat_closes = [100.0] * 40  # constant close, near-zero daily band -- ATR ~= 0.02% of price
    candles = _synthetic_candles(flat_closes, band=0.0001)
    price = Decimal("100")
    result = _atr_stop_loss_price(price, candles, Decimal("2"), AssetClass.EQUITY, Decimal("0.5"), Decimal("25"))
    assert result is None


def test_atr_stop_loss_falls_back_when_distance_is_pathologically_large() -> None:
    """An oversized ATR multiplier pushes the stop distance beyond the
    configured max_stop_distance_pct -- not a meaningful protective level,
    falls back rather than accepting it."""
    candles = _synthetic_candles(_BULLISH_CLOSES)
    price = Decimal("199.55")
    result = _atr_stop_loss_price(price, candles, Decimal("2"), AssetClass.EQUITY, Decimal("0.5"), Decimal("0.1"))
    assert result is None
