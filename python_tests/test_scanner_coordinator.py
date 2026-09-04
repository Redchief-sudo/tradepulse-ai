import asyncio
import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, reserve_symbol_for_execution
from tradepulse.models import AssetClass, AssetIdentity, Candle, ExecutionMode, Holding, RiskLimits, ScanRun, ScanRunStatus, ScanTrigger, SessionState, TradingSession, asset_identity_key
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider, AnthropicAIProvider, MarketDataCapabilities
from tradepulse.providers.anthropic_ai import SCAN_TOOL_NAME
from tradepulse.risk import load_session, save_session
from tradepulse.scanner import run_scan_cycle
from tradepulse.scanner.coordinator import REGIME_UNAVAILABLE_MULTIPLIER, _atr_stop_loss_price, _stop_loss_price
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


async def _setup(tmp_path, *, risk_limits: RiskLimits | None = None):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    ai_provider = AnthropicAIProvider("key", "claude-haiku-4-5", 10)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    limits = risk_limits or risk_limits_for_profile("balanced")
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


def _mock_quote_for(symbol: str, bid: str = "199.50", ask: str = "199.60") -> None:
    """Multi-candidate ranking tests need more than one symbol's quote --
    _mock_quote above stays AAPL-only (every existing single-candidate test
    relies on that), this is the generic version."""
    respx.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": symbol, "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
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


def _mock_multi_symbol_dynamic_full_fill(price: str = "199.60") -> list[str]:
    """Like _mock_dynamic_full_fill, but tracks orders for MULTIPLE symbols
    submitted within the same test (needed for cross-opportunity ranking
    tests, which execute more than one candidate per cycle). Returns the
    list this function appends each submitted order's symbol to, IN
    SUBMISSION ORDER -- the direct way to assert ranking actually changed
    execution order, not just that both candidates eventually filled."""
    import json as _json

    call_order: list[str] = []
    order_state: dict[str, str] = {}  # order_id -> submitted qty

    def _accept(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        symbol = body["symbol"]
        order_id = f"order-{symbol}"
        call_order.append(symbol)
        order_state[order_id] = body["qty"]
        return httpx.Response(200, json=_order_json("accepted", "0", None) | {"id": order_id, "symbol": symbol})

    def _status(request: httpx.Request) -> httpx.Response:
        order_id = request.url.path.rsplit("/", 1)[-1]
        symbol = order_id.removeprefix("order-")
        return httpx.Response(200, json=_order_json("filled", order_state[order_id], price) | {"id": order_id, "symbol": symbol})

    def _activities(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {
                "id": f"act-{order_id}", "activity_type": "FILL", "symbol": order_id.removeprefix("order-"),
                "side": "buy", "qty": qty, "price": price, "transaction_time": QUOTE_TS, "order_id": order_id,
            }
            for order_id, qty in order_state.items()
        ])

    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept)
    respx.get(url__regex=r".*/v2/orders/order-\w+$").mock(side_effect=_status)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(side_effect=_activities)
    return call_order


def _synthetic_closes(n: int, trend: float, amplitude: float, period: float, phase: float) -> list[float]:
    price = 100.0
    closes = []
    for i in range(n):
        price += trend + amplitude * math.sin(i / period + phase)
        closes.append(price)
    return closes


def _bars_json(closes: list[float], volumes: list[float] | None = None, band: float = 0.006) -> dict:
    """Daily bars, most-recent-first (matches the `sort=desc` the client
    requests) -- oldest day first in `closes`, so build newest-first here.
    `volumes`/`band` are optional overrides (default: constant 1,000,000
    volume, symmetric 0.6% high/low band) -- used by Strategy Sophistication
    Phase 1 ranking tests to give one candidate a deliberately different
    liquidity/risk_quality profile than another, real inputs rather than a
    hand-picked composite_score."""
    end = NOW
    volumes = volumes if volumes is not None else [1_000_000.0] * len(closes)
    rows = []
    for offset, (close, volume) in enumerate(zip(closes, volumes)):
        day = end - timedelta(days=len(closes) - 1 - offset)
        rows.append({
            "t": day.isoformat().replace("+00:00", "Z"), "o": close * (1 - band / 3), "h": close * (1 + band),
            "l": close * (1 - band), "c": close, "v": volume,
        })
    return {"bars": list(reversed(rows))}  # newest-first


# Empirically verified (via strategy.compute_real_factors/weighted_composite
# with the default strategy weights) to produce a BUY deterministic signal.
_BULLISH_CLOSES = _synthetic_closes(40, trend=0.4, amplitude=2.0, period=4.0, phase=0.5)
# A choppy, mildly declining series -- deterministic signal lands in
# HOLD/SELL/STRONG_SELL, never BUY/STRONG_BUY.
_BEARISH_CLOSES = _synthetic_closes(40, trend=-0.3, amplitude=3.0, period=2.0, phase=0.0)
# A separate fixture for Market Regime Phase 2 benchmark tests -- a smooth,
# genuinely low-volatility uptrend. Empirically verified (via
# classify_regime(calendar="equity") directly) to land on low_vol_bull, NOT
# _BULLISH_CLOSES above, whose sinusoidal noise pushes its realized vol
# (0.195) just past the real calibrated equity high-vol threshold (0.18) --
# a real, correct classification for THAT series, just not the one this
# fixture is for. Never assume a fixture built for the deterministic
# factor gate also produces a specific regime label without checking.
_REGIME_LOW_VOL_BULL_CLOSES = [100 + i * 0.5 for i in range(60)]


def _mock_bars(closes: list[float]) -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(closes))
    )


def _mock_bars_for(symbol: str, closes: list[float], volumes: list[float] | None = None, band: float = 0.006) -> None:
    respx.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(closes, volumes, band))
    )


# Strategy Sophistication Phase 1 -- a second BUY-signal fixture, empirically
# verified (via compute_real_factors/weighted_composite -- WITH _BULLISH_CLOSES
# supplied as benchmark_closes, matching the default _mock_spy_bars() fixture
# every test below reuses as its regime benchmark, and against the
# "transition" regime-conditioned weight profile that benchmark actually
# classifies to) to produce a composite score NOTABLY HIGHER than
# _BULLISH_CLOSES's own composite against that same benchmark (~68.5 vs.
# ~65.7) while still landing on BUY -- needed to test that cross-opportunity
# ranking genuinely reorders execution by composite, not just AI-return
# order. Price shape alone gives only a slim margin here (relative_strength
# scales with excess momentum over the benchmark, but technical_score is
# RSI-mean-reversion-flavored and penalizes a much steeper trend) -- the
# real separation comes from _STRONGER_BULLISH_VOLUMES (rising, elevated
# volume -> higher liquidity_score) and _STRONGER_BULLISH_BAND (a tighter
# high/low range -> higher ATR-based risk_quality_score) below, paired via
# _mock_bars_for's volumes/band overrides. Never assume relative composite
# ordering between two fixtures without checking directly.
_STRONGER_BULLISH_CLOSES = _synthetic_closes(40, trend=0.7, amplitude=2.0, period=4.0, phase=0.5)
_STRONGER_BULLISH_VOLUMES = [300_000.0 + i * 50_000.0 for i in range(40)]
_STRONGER_BULLISH_BAND = 0.0008

# A real, verified liquidity_crisis-classifying series (crash-shaped: steep
# decline, high realized vol) -- constructed via repeated random daily
# returns and checked directly against classify_regime(calendar="equity")
# until one actually produced "liquidity_crisis" (position_multiplier=0.0),
# same discipline as _REGIME_LOW_VOL_BULL_CLOSES above. Do not replace with
# a fixture that "looks like a crash" without re-verifying the label.
_REGIME_LIQUIDITY_CRISIS_CLOSES = [
    100.0, 101.51074962440077, 101.6399428092463, 96.28264909979139, 88.71740170613893, 85.32874787545875,
    80.61770179771135, 81.05366412922953, 75.26076168583758, 71.96851824940211, 70.0499186119367,
    71.82204577024578, 69.00302307112158, 63.83428642729546, 63.89356758420021, 62.54790767906409,
    57.54914235958569, 59.02006396376602, 61.2183061534585, 61.808129703691186, 63.31294455189201,
    58.85720814314001, 58.66728062465411, 60.06439064191993, 59.42995625758801, 56.78786885494842,
    50.888301711926005, 48.31678828367774, 47.24134913941894, 48.4734870721543, 50.153433627015445,
    47.96281009861746, 48.84770420552889, 45.021891913000026, 45.418285013798034, 43.95544783109865,
    38.779547566409924, 38.59157339766798, 36.42318107034235, 36.85935581512247, 36.37666457062264,
    32.01811633492645, 30.70449174254832, 31.28224109439134, 28.749184780076806, 26.795180257013484,
    27.31166799783523, 24.86920559304305, 24.14306752743878, 22.167646700574892, 22.939223566428737,
    23.134406884021534, 22.016439711899146, 19.657847828749052, 18.3055596437861, 17.59659452287791,
    18.111354950075995, 16.254022013306702, 15.737188964217223, 15.627812756014993,
]

UNIVERSE_TWO_EQUITIES = ExecutableUniverse(equities=frozenset({"AAPL", "MSFT"}), crypto=frozenset())


def _mock_spy_bars(closes: list[float] | None = None) -> respx.Route:
    """Market Regime Phase 2's benchmark fetch for the equity AND options
    lanes (options inherit the equity/broad-market regime -- see
    scanner/coordinator.py::_BENCHMARK_ASSETS). Every test that reaches
    past run_scan_cycle's SESSION_BLOCKED gate for AssetClass.EQUITY/OPTION
    needs this mocked -- the benchmark fetch happens once per cycle,
    independent of any candidate. (Crypto's own benchmark is BTC/USD,
    already satisfied by the existing _mock_crypto_bars -- same URL,
    keyed the same way, regardless of which symbol's candles a given test
    was originally mocking it for.)"""
    return respx.get("https://data.alpaca.markets/v2/stocks/SPY/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(closes or _BULLISH_CLOSES))
    )


@respx.mock
async def test_equity_lane_prompt_contains_only_equity_symbols(tmp_path) -> None:
    """The AI is never shown the other lane's universe -- proves the actual
    request body, not just that the right endpoint got called."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    universe = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset({"BTC/USD"}))
    captured: dict[str, bytes] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_tool_use_response([]))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    prompt = captured["body"].decode()
    assert "AAPL" in prompt
    assert "BTC/USD" not in prompt
    assert "equity" in prompt.lower() or "ETF" in prompt


@respx.mock
async def test_scan_run_stamps_resolved_market_data_capabilities_when_provided(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))
    capabilities = MarketDataCapabilities(equity_feed="sip", option_feed="opra")

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY,
        clock=lambda: NOW, capabilities=capabilities,
    )
    await broker.aclose()
    await ai_provider.aclose()

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.market_data_tier == "algo_trader_plus"
    assert scan_run.equity_feed == "sip"
    assert scan_run.option_feed == "opra"


@respx.mock
async def test_scan_run_leaves_capability_fields_none_when_omitted(tmp_path) -> None:
    """Every EXISTING call site (this codebase's ~20 direct run_scan_cycle
    calls in tests, and any caller not yet capability-aware) omits
    `capabilities` entirely -- must stay a true no-op, never crash or stamp
    a guessed value."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.market_data_tier is None
    assert scan_run.equity_feed is None
    assert scan_run.option_feed is None


@respx.mock
async def test_crypto_lane_prompt_contains_only_crypto_symbols(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_crypto_bars(_BULLISH_CLOSES)
    universe = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset({"BTC/USD"}))
    captured: dict[str, bytes] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_tool_use_response([]))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, limits, AssetClass.CRYPTO, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    prompt = captured["body"].decode()
    assert "BTC/USD" in prompt
    assert "AAPL" not in prompt
    assert "crypto" in prompt.lower()
    assert "24/7" in prompt or "continuously" in prompt.lower()


@respx.mock
async def test_candidate_outside_requested_lane_is_rejected(tmp_path, caplog) -> None:
    """A candidate the AI reports for a symbol outside the requested lane
    (here: the crypto lane's AI hallucinates an equity ticker) is rejected
    OUTSIDE_SCAN_LANE and never reaches order placement -- defense in depth,
    not just trust in the prompt text."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_crypto_bars(_BULLISH_CLOSES)
    universe = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset({"BTC/USD"}))
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "hallucinated"}])
        )
    )
    _mock_account()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, limits, AssetClass.CRYPTO, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 0
    assert order_route.call_count == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "OUTSIDE_SCAN_LANE"
    assert rejected[0].asset_class == "crypto"


@respx.mock
async def test_candidate_rejection_is_persisted_alongside_the_log_line(tmp_path) -> None:
    """A rejection must survive the process, not just the log stream --
    same scenario as test_candidate_outside_requested_lane_is_rejected,
    but asserting the durable rejected_candidates row instead of caplog."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_crypto_bars(_BULLISH_CLOSES)
    universe = ExecutableUniverse(equities=frozenset({"AAPL"}), crypto=frozenset({"BTC/USD"}))
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "hallucinated"}])
        )
    )
    _mock_account()
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, limits, AssetClass.CRYPTO, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    rows = await repositories.rejected_candidates.list_all()
    assert len(rows) == 1
    rejection = hydrate("rejected_candidates", rows[0]["payload"])
    assert rejection.symbol == "AAPL"
    assert rejection.reason == "OUTSIDE_SCAN_LANE"
    assert rejection.asset_class == AssetClass.CRYPTO
    assert rejection.scan_run_id == summary.scan_run_id
    assert rejection.occurred_at == NOW


def _mock_crypto_quote(bid: str = "60000", ask: str = "60010") -> None:
    respx.get("https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes").mock(
        return_value=httpx.Response(200, json={"quotes": {"BTC/USD": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}}})
    )


def _mock_crypto_bars(closes: list[float]) -> None:
    end = NOW
    rows = []
    for offset, close in enumerate(closes):
        day = end - timedelta(days=len(closes) - 1 - offset)
        rows.append({
            "t": day.isoformat().replace("+00:00", "Z"), "o": close * 0.998, "h": close * 1.006,
            "l": close * 0.994, "c": close, "v": 100.0,
        })
    respx.get("https://data.alpaca.markets/v1beta3/crypto/us/bars").mock(
        return_value=httpx.Response(200, json={"bars": {"BTC/USD": list(reversed(rows))}})  # newest-first, matches sort=desc
    )


def _mock_crypto_dynamic_full_fill(price: str = "60010") -> respx.Route:
    """Crypto counterpart of _mock_dynamic_full_fill -- wires order
    placement -> status polling -> fill activity together so the mocked
    fill reflects whatever quantity the scanner's real sizing computed."""
    import json as _json

    state: dict[str, str] = {}

    def _accept(request: httpx.Request) -> httpx.Response:
        state["qty"] = _json.loads(request.content)["qty"]
        return httpx.Response(200, json={
            "id": "order-1", "status": "accepted", "symbol": "BTC/USD", "side": "buy",
            "filled_qty": "0", "filled_avg_price": None, "submitted_at": QUOTE_TS,
        })

    def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "order-1", "status": "filled", "symbol": "BTC/USD", "side": "buy",
            "filled_qty": state["qty"], "filled_avg_price": price, "submitted_at": QUOTE_TS,
        })

    def _activities(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "act-btc-1", "activity_type": "FILL", "symbol": "BTC/USD", "side": "buy",
            "qty": state["qty"], "price": price, "transaction_time": QUOTE_TS, "order_id": "order-1",
        }])

    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept)
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(side_effect=_status)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(side_effect=_activities)
    return order_route


@respx.mock
async def test_full_crypto_scan_cycle_executes_ai_recommended_buy(tmp_path) -> None:
    """Proves the WHOLE chain stays asset-agnostic end to end for the
    crypto lane, not just AI discovery -- crypto prompt -> crypto candidate
    -> authoritative crypto quote/bars -> ATR stop -> confidence-adjusted
    dynamic sizing -> the session gate's CONTINUOUS_ASSET_SESSION exemption
    actually engaging at the submission-boundary clock check (crypto BUYs
    never call get_clock there -- see gateway.py's
    `asset_class == AssetClass.EQUITY` guard) -> canonical crypto asset
    identity -> execution reservation -> gateway -> mocked broker fill ->
    settlement -> a Holding row keyed correctly for the crypto asset.
    Mirrors the equity-lane test's structure with AssetClass.CRYPTO and a
    crypto-only universe. (/v2/clock is still mocked here because
    sync_market_session polls it unconditionally for any ACTIVE session,
    regardless of which lane is scanning -- unrelated to the crypto
    exemption this test is actually proving.)"""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    crypto_universe = ExecutableUniverse(equities=frozenset(), crypto=frozenset({"BTC/USD"}))
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "BTC/USD", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_crypto_quote()
    _mock_crypto_bars(_BULLISH_CLOSES)
    order_route = _mock_crypto_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, crypto_universe, limits, AssetClass.CRYPTO, clock=lambda: NOW)
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
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.asset_class == AssetClass.CRYPTO

    opp_rows = await repositories.opportunities.list_all()
    assert len(opp_rows) == 1
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.asset.symbol == "BTC/USD"
    assert opportunity.asset.asset_class == AssetClass.CRYPTO

    btc_asset = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
    holding_row = await repositories.holdings.get(asset_identity_key(btc_asset))
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity > 0
    assert holding.stop_loss is not None
    assert holding.stop_loss < Decimal("60005")  # mid of the mocked 60000/60010 bid/ask -- a real ATR-based protective distance


def _mock_options_quote(occ_symbol: str, bid: str = "2.00", ask: str = "2.02") -> None:
    respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(200, json={"quotes": {occ_symbol: {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}}})
    )


def _mock_options_chain(occ_symbol: str, underlying: str = "AAPL", strike: str = "205", days_out: int = 30) -> None:
    """One eligible call contract, comfortably inside the balanced
    profile's [21, 45]-day window, near the 3%-OTM target off a ~199.55
    mid quote (spot*1.03 ~= 205.5, so strike 205 wins)."""
    expiry = (NOW + timedelta(days=days_out)).date().isoformat()
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(200, json={
            "option_contracts": [{
                "symbol": occ_symbol, "underlying_symbol": underlying, "type": "call",
                "strike_price": strike, "expiration_date": expiry, "multiplier": "100",
                "status": "active", "tradable": True,
            }],
            "next_page_token": None,
        })
    )


def _mock_options_dynamic_full_fill(occ_symbol: str, price: str = "2.02") -> respx.Route:
    """Options counterpart of _mock_dynamic_full_fill -- whole-contract qty,
    OCC symbol, but otherwise the same accept -> status -> activity wiring
    so the mocked fill reflects whatever quantity the scanner's real sizing
    actually computed."""
    import json as _json

    state: dict[str, str] = {}

    def _accept(request: httpx.Request) -> httpx.Response:
        state["qty"] = _json.loads(request.content)["qty"]
        return httpx.Response(200, json={
            "id": "order-1", "status": "accepted", "symbol": occ_symbol, "side": "buy",
            "filled_qty": "0", "filled_avg_price": None, "submitted_at": QUOTE_TS,
        })

    def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "order-1", "status": "filled", "symbol": occ_symbol, "side": "buy",
            "filled_qty": state["qty"], "filled_avg_price": price, "submitted_at": QUOTE_TS,
        })

    def _activities(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "act-opt-1", "activity_type": "FILL", "symbol": occ_symbol, "side": "buy",
            "qty": state["qty"], "price": price, "transaction_time": QUOTE_TS, "order_id": "order-1",
        }])

    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=_accept)
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(side_effect=_status)
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(side_effect=_activities)
    return order_route


OPTIONS_UNIVERSE = ExecutableUniverse(equities=frozenset(), crypto=frozenset(), options_underlyings=frozenset({"AAPL"}))


@respx.mock
async def test_options_lane_prompt_never_names_a_specific_contract(tmp_path) -> None:
    """The AI is only ever shown underlying tickers -- it never sees or
    reasons about specific contracts, matching the module's own division of
    responsibility (see _build_scan_prompt's options branch docstring)."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    captured: dict[str, bytes] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json=_tool_use_response([]))

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    prompt = captured["body"].decode()
    assert "AAPL" in prompt
    assert "never choose a specific" in prompt.lower()  # explicit division-of-responsibility language, not just absence of the word "contract"


@respx.mock
async def test_option_candidate_outside_requested_lane_is_rejected(tmp_path, caplog) -> None:
    """The options lane's AI hallucinates a crypto pair -- rejected
    OUTSIDE_SCAN_LANE and never reaches contract resolution or order
    placement, same defense-in-depth principle as every other lane."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "BTC/USD", "recommendation": "BUY", "confidence": 90, "summary": "hallucinated"}])
        )
    )
    _mock_account()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 0
    assert order_route.call_count == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "OUTSIDE_SCAN_LANE"
    assert rejected[0].asset_class == "option"


@respx.mock
async def test_options_scan_cycle_rejects_when_no_eligible_contract(tmp_path, caplog) -> None:
    """select_contract returning None (empty/ineligible chain) is a clean,
    fail-closed rejection -- never a crash, never a fabricated contract."""
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
    _mock_spy_bars()
    respx.get("https://paper-api.alpaca.markets/v2/options/contracts").mock(
        return_value=httpx.Response(200, json={"option_contracts": [], "next_page_token": None})
    )

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert any(r.reason == "NO_ELIGIBLE_OPTION_CONTRACT" for r in rejected)


@respx.mock
async def test_deterministic_gate_fetches_candles_for_underlying_not_contract(tmp_path) -> None:
    """The composite/momentum gate must run on the UNDERLYING's own candle
    history -- a contract's own price action is too short-lived/decay-driven
    to be a meaningful technical signal. Proven by asserting the ONLY bars
    request made is for AAPL's plain stock-bars endpoint; if the code
    incorrectly tried to fetch candles for the OCC contract symbol instead,
    no mock would match that URL and respx would fail this test."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    bars_route = respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json=_bars_json(_BULLISH_CLOSES))
    )
    _mock_spy_bars()
    occ_symbol = "AAPL" + (NOW + timedelta(days=30)).date().strftime("%y%m%d") + "C00205000"
    _mock_options_chain(occ_symbol)
    _mock_options_quote(occ_symbol)
    order_route = _mock_options_dynamic_full_fill(occ_symbol)

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert bars_route.call_count == 1
    assert summary.orders_submitted == 1
    assert order_route.call_count == 1
    submitted_symbol = order_route.calls[0].request.content
    assert occ_symbol.encode() in submitted_symbol


@respx.mock
async def test_full_options_scan_cycle_executes_ai_recommended_buy(tmp_path) -> None:
    """Proves the WHOLE chain stays asset-agnostic end to end for the
    options lane -- options prompt -> underlying candidate -> underlying
    quote/bars/deterministic gate -> options chain -> deterministic
    (non-AI) contract selection -> the contract's own premium quote ->
    flat pct-of-premium stop (never ATR, never Greeks) -> multiplier-aware
    dynamic sizing -> equity-style market-hours gating (never crypto's
    continuous exemption) -> canonical options contract identity ->
    execution reservation -> gateway -> mocked broker fill -> settlement ->
    a Holding row keyed correctly for the resolved contract, not the
    underlying."""
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
    _mock_spy_bars()
    occ_symbol = "AAPL" + (NOW + timedelta(days=30)).date().strftime("%y%m%d") + "C00205000"
    _mock_options_chain(occ_symbol)
    _mock_options_quote(occ_symbol)
    order_route = _mock_options_dynamic_full_fill(occ_symbol)

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
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
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.asset_class == AssetClass.OPTION

    opp_rows = await repositories.opportunities.list_all()
    assert len(opp_rows) == 1
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.asset.symbol == occ_symbol  # the resolved CONTRACT, not "AAPL"
    assert opportunity.asset.asset_class == AssetClass.OPTION
    assert opportunity.asset.metadata["underlying_symbol"] == "AAPL"

    contract_asset = AssetIdentity(
        occ_symbol, AssetClass.OPTION, f"alpaca:{occ_symbol}",
        metadata={"underlying_symbol": "AAPL", "contract_multiplier": "100"},
    )
    holding_row = await repositories.holdings.get(asset_identity_key(contract_asset))
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity > 0  # whole contracts
    assert holding.quantity == holding.quantity.to_integral_value()
    assert holding.stop_loss is not None
    assert holding.stop_loss < Decimal("2.02")  # a real pct-of-premium protective distance below the mocked ask


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
    _mock_spy_bars()
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    persisted_scan_run = hydrate("scan_runs", scan_row["payload"])
    assert persisted_scan_run.universe_size == len(UNIVERSE.equities)  # the CONFIGURED universe, not candidates_discovered

    ai_rows = await repositories.ai_responses.list_all()
    assert len(ai_rows) == 1
    ai_response = hydrate("ai_responses", ai_rows[0]["payload"])
    assert persisted_scan_run.ai_response_request_id == ai_response.request_id  # exact linkage, not a guess/join
    assert ai_response.result["candidates"] == [{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90.0, "summary": "Strong momentum."}]

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


# ---- Market Regime Phase 2 -----------------------------------------------


@respx.mock
async def test_regime_valid_benchmark_produces_real_calibrated_classification(tmp_path) -> None:
    """The success path: a real SPY benchmark fetch produces a genuine
    calibrated regime, persisted on both ScanRun and the approved
    TradeIntent's risk_snapshot -- no "unavailable" reason present."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    _mock_spy_bars(_REGIME_LOW_VOL_BULL_CLOSES)  # a real, low-vol steady uptrend -- low_vol_bull, multiplier 1.0
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert order_route.call_count == 1

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "low_vol_bull"
    assert scan_run.regime_reason is None
    assert scan_run.regime_position_multiplier == Decimal("1.0")
    assert scan_run.regime_confidence is not None
    assert scan_run.regime_realized_vol is not None

    intent_rows = await repositories.trade_intents.list_all()
    assert len(intent_rows) == 1
    intent = hydrate("trade_intents", intent_rows[0]["payload"])
    assert intent.risk_snapshot["regime"] == "low_vol_bull"
    assert intent.risk_snapshot["regime_position_multiplier"] == "1.0"
    assert intent.risk_snapshot["regime_timeframe"] == "1day"
    assert intent.risk_snapshot["regime_calendar"] == "equity"


@respx.mock
async def test_regime_benchmark_http_failure_falls_back_to_unavailable_conservative_multiplier(tmp_path) -> None:
    """Direct regression test for the original fail-open defect: a
    benchmark HTTP failure must never behave as "no regime reduction" --
    the resulting TradeIntent must actually carry the conservative
    REGIME_UNAVAILABLE_MULTIPLIER (0.5), not a multiplier of 1.0 or None,
    and the scan cycle itself must not fail."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/bars").mock(return_value=httpx.Response(500, json={"message": "internal error"}))
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED  # never fails the cycle over a regime-infra hiccup
    assert order_route.call_count == 1  # trading still proceeds, at the conservative fallback size

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "unavailable"
    assert scan_run.regime_reason == "benchmark_fetch_failed"
    assert scan_run.regime_position_multiplier == REGIME_UNAVAILABLE_MULTIPLIER

    intent_rows = await repositories.trade_intents.list_all()
    assert len(intent_rows) == 1
    intent = hydrate("trade_intents", intent_rows[0]["payload"])
    assert intent.risk_snapshot["regime"] == "unavailable"
    assert intent.risk_snapshot["regime_reason"] == "benchmark_fetch_failed"
    assert intent.risk_snapshot["regime_position_multiplier"] == str(REGIME_UNAVAILABLE_MULTIPLIER)
    assert any("RISK_BUDGET_SCALED_BY_REGIME_TO_50.0PCT" in r for r in intent.risk_snapshot["reasons"])


@respx.mock
async def test_regime_benchmark_insufficient_history_also_falls_back_to_unavailable(tmp_path) -> None:
    """Same underlying cause class as an HTTP failure -- fetch_candles's own
    MIN_CANDLES gate raises the exact same ProviderDataFailure, caught by
    the exact same except clause -- proving this explicitly rather than
    assuming it, per review."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/bars").mock(return_value=httpx.Response(200, json={"bars": []}))
    _mock_account()  # reached unconditionally after the AI call, regardless of candidate count

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "unavailable"
    assert scan_run.regime_reason == "benchmark_fetch_failed"
    assert scan_run.regime_position_multiplier == REGIME_UNAVAILABLE_MULTIPLIER


@respx.mock
async def test_regime_benchmark_malformed_data_falls_back_to_unavailable_with_distinct_reason(tmp_path) -> None:
    """A non-numeric field in Alpaca's raw bars response (get_bars's own
    unguarded Decimal(str(value)) parsing raises decimal.InvalidOperation)
    -- distinct, truthful reason from a clean HTTP/provider-level failure,
    never a blanket except swallowing both into the same label."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/bars").mock(
        return_value=httpx.Response(200, json={"bars": [
            {"t": NOW.isoformat().replace("+00:00", "Z"), "o": 1.0, "h": 1.0, "l": 1.0, "c": "not-a-number", "v": 1.0}
        ]})
    )
    _mock_account()  # reached unconditionally after the AI call, regardless of candidate count

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "unavailable"
    assert scan_run.regime_reason == "benchmark_data_invalid"
    assert scan_run.regime_position_multiplier == REGIME_UNAVAILABLE_MULTIPLIER


@respx.mock
async def test_options_lane_benchmark_fetch_requests_spy_never_the_contract(tmp_path) -> None:
    """Options inherit the equity/broad-market regime -- the benchmark
    fetch must request SPY, never the resolved OCC contract symbol, and
    must succeed even though the contract itself is only resolved deep
    inside the per-candidate loop, well after the benchmark fetch starts."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    spy_route = _mock_spy_bars(_REGIME_LOW_VOL_BULL_CLOSES)
    occ_symbol = "AAPL" + (NOW + timedelta(days=30)).date().strftime("%y%m%d") + "C00205000"
    _mock_options_chain(occ_symbol)
    _mock_options_quote(occ_symbol)
    _mock_options_dynamic_full_fill(occ_symbol)

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert spy_route.call_count == 1
    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "low_vol_bull"  # a real classification, resolved from SPY -- proves the fetch actually targeted SPY, not the contract


@respx.mock
async def test_opportunity_metadata_carries_resolved_market_data_provenance(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    broker.set_market_data_feeds(equity_feed="sip", option_feed="opra")
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    _mock_spy_bars()
    _mock_dynamic_full_fill()

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["market_data_provider"] == "alpaca"
    assert opportunity.metadata["market_data_feed"] == "sip"
    assert opportunity.metadata["market_data_authority"] == "consolidated"


@respx.mock
async def test_mixed_capability_provenance_is_independently_correct_per_lane(tmp_path) -> None:
    """SIP rejected, OPRA entitled is a real, expected resolved state now
    that the two feeds are probed independently (see
    market_data_capability.py). Equity and options Opportunities against
    the SAME broker must each carry THEIR OWN feed's provenance, not a
    single account-wide label."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    broker.set_market_data_feeds(equity_feed="iex", option_feed="opra")
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_account()
    _mock_positions()
    _mock_spy_bars()  # shared benchmark for both lanes below -- options inherits the equity/broad-market regime

    # ---- Equity lane ----
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    _mock_dynamic_full_fill()
    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)

    # ---- Options lane ----
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    occ_symbol = "AAPL" + (NOW + timedelta(days=30)).date().strftime("%y%m%d") + "C00205000"
    _mock_options_chain(occ_symbol)
    _mock_options_quote(occ_symbol)
    _mock_options_dynamic_full_fill(occ_symbol)
    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, OPTIONS_UNIVERSE, limits, AssetClass.OPTION, clock=lambda: NOW)

    await broker.aclose()
    await ai_provider.aclose()

    opp_rows = await repositories.opportunities.list_all()
    assert len(opp_rows) == 2
    opportunities = {o.asset.asset_class: o for o in (hydrate("opportunities", r["payload"]) for r in opp_rows)}

    equity_opp = opportunities[AssetClass.EQUITY]
    assert equity_opp.metadata["market_data_feed"] == "iex"
    assert equity_opp.metadata["market_data_authority"] == "exchange_limited"

    option_opp = opportunities[AssetClass.OPTION]
    assert option_opp.metadata["market_data_feed"] == "opra"
    assert option_opp.metadata["market_data_authority"] == "consolidated"


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
    _mock_spy_bars()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    _mock_spy_bars()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    _mock_spy_bars()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 0  # must never spend on an AI call while the kill switch is active
    assert summary.status == ScanRunStatus.FAILED
    assert summary.error == "SESSION_BLOCKED"

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    persisted_scan_run = hydrate("scan_runs", scan_row["payload"])
    assert persisted_scan_run.universe_size == len(UNIVERSE.equities)  # computed before the session gate, so still set on an early failure
    assert persisted_scan_run.ai_response_request_id is None  # the AI was never called -- nothing to link


@respx.mock
async def test_scan_cycle_marks_failed_when_ai_provider_errors(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()  # defensive -- the regime benchmark fetch races the AI call; mock it so this test never depends on cancellation timing
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(429, json={"error": {"message": "rate limited"}}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.FAILED
    assert summary.error is not None
    assert (await repositories.ai_responses.list_all()) == []

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    assert scan_row["status"] == "failed"


@respx.mock
async def test_scan_cycle_links_ai_response_even_when_broker_becomes_unavailable_after_ai_succeeds(tmp_path) -> None:
    """The AI call and its persistence both complete successfully before the
    broker outage -- the resulting FAILED ScanRun must still link the real
    AIResponse that was actually obtained, not leave it None just because
    the cycle later failed for an unrelated reason."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    _mock_spy_bars()
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Strong momentum."}])
        )
    )
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(return_value=httpx.Response(500, json={"message": "internal error"}))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1
    assert summary.status == ScanRunStatus.FAILED
    assert summary.error is not None and summary.error.startswith("BROKER_UNAVAILABLE")

    ai_rows = await repositories.ai_responses.list_all()
    assert len(ai_rows) == 1  # the AI response was persisted before the broker outage
    ai_response = hydrate("ai_responses", ai_rows[0]["payload"])

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    persisted_scan_run = hydrate("scan_runs", scan_row["payload"])
    assert persisted_scan_run.ai_response_request_id == ai_response.request_id  # linked despite the later failure


@respx.mock
async def test_stale_running_scan_run_is_reclaimed_as_failed(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(
        repositories,
        TradingSession("session", SessionState.RISK_STOPPED, False, NOW, kill_switch_reason="daily loss", kill_switch_reset_required=True),
    )
    stale = ScanRun(
        scan_run_id="stale-1", scan_generation="gen-stale", trigger=ScanTrigger.SCHEDULED, asset_class=AssetClass.EQUITY,
        status=ScanRunStatus.RUNNING, started_at=NOW - timedelta(seconds=1000), lock_owner_token="owner-1",
    )
    await repositories.scan_runs.create_once("stale-1", stale, status=stale.status.value)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
        scan_run_id="recent-1", scan_generation="gen-recent", trigger=ScanTrigger.SCHEDULED, asset_class=AssetClass.EQUITY,
        status=ScanRunStatus.RUNNING, started_at=NOW - timedelta(seconds=5), lock_owner_token="owner-1",
    )
    await repositories.scan_runs.create_once("recent-1", recent, status=recent.status.value)

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    recent_row = await repositories.scan_runs.get("recent-1")
    assert recent_row["status"] == "running"


@respx.mock
async def test_scan_cycle_syncs_session_to_market_closed_before_evaluating_candidates(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open(is_open=False)
    _mock_spy_bars()
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    _mock_spy_bars()
    ai_route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json=_tool_use_response([])))

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    _mock_spy_bars()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    database = repositories.trade_intents.database
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    assert await reserve_symbol_for_execution(database, asset, "another-coordinator") is True

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
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
    _mock_spy_bars()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    lease_lost = asyncio.Event()
    lease_lost.set()  # already lost before the loop even starts

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(
            repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW, lease_lost=lease_lost,
        )
    await broker.aclose()
    await ai_provider.aclose()

    assert ai_route.call_count == 1  # discovery still ran
    assert summary.candidates_approved == 0
    assert order_route.call_count == 0
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "COMMAND_LEASE_LOST"


# ---- Strategy Sophistication Phase 1 --------------------------------------


@respx.mock
async def test_multiple_candidates_execute_in_composite_rank_order_not_ai_return_order(tmp_path) -> None:
    """The AI returns AAPL (weaker composite, ~68.5) before MSFT (stronger
    composite, ~72.8) -- both real, calibrated BUY signals under the
    "transition" regime-conditioned weight profile (the default _mock_spy_bars()
    benchmark classifies to "transition"). Cross-opportunity ranking must
    submit MSFT FIRST despite AI-return order, proving pass 2 genuinely
    executes best-edge-first rather than preserving AI order."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."},
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger setup."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_spy_bars()
    call_order = _mock_multi_symbol_dynamic_full_fill()

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE_TWO_EQUITIES, limits, AssetClass.EQUITY, clock=lambda: NOW,
    )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_approved == 2
    assert summary.orders_submitted == 2
    assert call_order == ["MSFT", "AAPL"]  # higher composite executes first, despite AI listing AAPL first

    opp_rows = await repositories.opportunities.list_all()
    opportunities = {hydrate("opportunities", row["payload"]).asset.symbol: hydrate("opportunities", row["payload"]) for row in opp_rows}
    assert opportunities["MSFT"].metadata["composite_rank"] == 1
    assert opportunities["AAPL"].metadata["composite_rank"] == 2
    assert opportunities["MSFT"].metadata["candidates_ranked_total"] == 2
    assert Decimal(opportunities["MSFT"].metadata["composite_score"]) > Decimal(opportunities["AAPL"].metadata["composite_score"])


@respx.mock
async def test_lower_ranked_candidate_still_executes_when_budget_and_cash_allow(tmp_path) -> None:
    """Ranking changes ORDER, not whether a candidate gets a chance -- with
    ample cash for both, the lower-ranked candidate must still fill."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger setup."},
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_spy_bars()
    call_order = _mock_multi_symbol_dynamic_full_fill()

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE_TWO_EQUITIES, limits, AssetClass.EQUITY, clock=lambda: NOW,
    )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.orders_submitted == 2
    assert set(call_order) == {"AAPL", "MSFT"}
    assert all(r.status == "filled" for r in summary.execution_results)


@respx.mock
async def test_pass_two_rejection_on_top_ranked_candidate_does_not_block_lower_ranked_one(tmp_path, caplog) -> None:
    """MSFT (higher composite, ranked first) has its symbol execution lock
    already held by another coordinator -- pass 2 must still reach AAPL
    (lower-ranked), not abort the whole cycle."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."},
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger setup."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_spy_bars()
    call_order = _mock_multi_symbol_dynamic_full_fill()

    database = repositories.trade_intents.database
    msft = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    assert await reserve_symbol_for_execution(database, msft, "another-coordinator") is True

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(
            repositories, ai_provider, market_data, broker, gateway, UNIVERSE_TWO_EQUITIES, limits, AssetClass.EQUITY, clock=lambda: NOW,
        )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.candidates_approved == 1
    assert call_order == ["AAPL"]  # MSFT (ranked first) was rejected in pass 2; AAPL still got its turn
    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected" and r.reason == "SYMBOL_EXECUTION_LOCKED"]
    assert len(rejected) == 1
    assert rejected[0].symbol == "MSFT"


@respx.mock
async def test_liquidity_crisis_regime_suppresses_all_new_entries_before_any_fetch(tmp_path, caplog) -> None:
    """The signal-layer gate: every candidate is rejected with
    LIQUIDITY_CRISIS_NEW_ENTRIES_SUPPRESSED, before any candle/quote fetch
    for that candidate (no order ever placed)."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_spy_bars(_REGIME_LIQUIDITY_CRISIS_CLOSES)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))
    # Deliberately NOT mocking AAPL's own quote/bars -- if the gate below
    # didn't fire before those fetches, this test would fail with a
    # respx AllMockedAssertionError instead of the expected rejection,
    # which is itself proof the suppression happens before any I/O for
    # the candidate.

    with caplog.at_level(logging.INFO):
        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert summary.candidates_approved == 0
    assert order_route.call_count == 0

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "liquidity_crisis"
    assert scan_run.regime_position_multiplier == Decimal("0.0")  # the risk/engine.py sizing-layer backstop value

    rejected = [r for r in caplog.records if getattr(r, "event", None) == "candidate_rejected"]
    assert len(rejected) == 1
    assert rejected[0].reason == "LIQUIDITY_CRISIS_NEW_ENTRIES_SUPPRESSED"


@respx.mock
async def test_low_vol_bull_regime_weight_profile_recorded_on_scan_run_and_opportunity(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    _mock_spy_bars(_REGIME_LOW_VOL_BULL_CLOSES)
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert order_route.call_count == 1

    scan_row = await repositories.scan_runs.get(summary.scan_run_id)
    scan_run = hydrate("scan_runs", scan_row["payload"])
    assert scan_run.regime == "low_vol_bull"
    assert scan_run.regime_weight_profile == "v1+regime:low_vol_bull"

    opp_rows = await repositories.opportunities.list_all()
    assert len(opp_rows) == 1
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["regime_weight_profile"] == "v1+regime:low_vol_bull"
    assert "liquidity_score" in opportunity.metadata
    assert "risk_quality_score" in opportunity.metadata
    assert opportunity.metadata["factor_breakdown"]["relative_strength"] != "unavailable"  # SPY benchmark was available this cycle


@respx.mock
async def test_relative_strength_factor_reuses_lane_regime_benchmark_without_duplicate_fetch(tmp_path) -> None:
    """The relative-strength factor consumes the SAME benchmark candles
    already fetched to classify the lane's regime -- the SPY bars route
    must be hit exactly once per cycle, not twice."""
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote()
    _mock_bars(_BULLISH_CLOSES)
    spy_route = _mock_spy_bars(_REGIME_LOW_VOL_BULL_CLOSES)
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert order_route.call_count == 1
    assert spy_route.call_count == 1

    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["relative_strength_score"] is not None


# ---- Portfolio Optimization -------------------------------------------------

UNIVERSE_THREE_EQUITIES = ExecutableUniverse(equities=frozenset({"AAPL", "MSFT", "JNJ"}), crypto=frozenset())


@respx.mock
async def test_same_sector_candidates_share_exposure_and_the_second_is_capped(tmp_path) -> None:
    """AAPL and MSFT are both mapped to "Technology" (config/sectors.py).
    With max_sector_pct tightened just above max_position_pct, MSFT (ranked
    first, higher composite) consumes nearly the whole Technology bucket,
    leaving no room for AAPL -- proving sector_for_symbol's real value now
    actually constrains risk/engine.py's pre-existing (but previously inert,
    everything-defaulted-to-"Other") max_sector_pct cap."""
    tight_limits = replace(risk_limits_for_profile("balanced"), max_sector_pct=Decimal("8"))  # just above max_position_pct=7
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path, risk_limits=tight_limits)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."},
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger setup."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_spy_bars()
    _mock_multi_symbol_dynamic_full_fill()

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE_TWO_EQUITIES, tight_limits, AssetClass.EQUITY, clock=lambda: NOW,
    )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    intent_rows = await repositories.trade_intents.list_all()
    intents = {hydrate("trade_intents", row["payload"]).asset.symbol: hydrate("trade_intents", row["payload"]) for row in intent_rows}
    assert intents["MSFT"].status.value != "rejected"
    # AAPL (ranked second, same "Technology" sector) must be constrained by
    # MSFT's already-approved exposure -- either sized down or rejected
    # outright, but never approved at the same full size MSFT got.
    aapl_reasons = intents["AAPL"].risk_snapshot.get("reasons", [])
    assert any("SECTOR" in r or "INSUFFICIENT_CAPACITY" in r for r in aapl_reasons)


@respx.mock
async def test_different_sector_candidate_is_unaffected_by_another_sectors_exhausted_cap(tmp_path) -> None:
    """The direct proof sectors are genuinely DISTINGUISHED now, not merged
    into one universal "Other" bucket the way every scanner-originated
    position was before this phase: JNJ (Healthcare) must execute at full,
    unconstrained size even though the Technology bucket (AAPL+MSFT) is
    exhausted by the same tight max_sector_pct in the same cycle."""
    tight_limits = replace(risk_limits_for_profile("balanced"), max_sector_pct=Decimal("8"))
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path, risk_limits=tight_limits)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."},
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger setup."},
                {"symbol": "JNJ", "recommendation": "BUY", "confidence": 90, "summary": "Different sector."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_quote_for("JNJ")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_bars_for("JNJ", _BULLISH_CLOSES)  # same composite as AAPL -- ties broken alphabetically, so JNJ ranks last
    _mock_spy_bars()
    _mock_multi_symbol_dynamic_full_fill()

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE_THREE_EQUITIES, tight_limits, AssetClass.EQUITY, clock=lambda: NOW,
    )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    intent_rows = await repositories.trade_intents.list_all()
    intents = {hydrate("trade_intents", row["payload"]).asset.symbol: hydrate("trade_intents", row["payload"]) for row in intent_rows}
    assert intents["MSFT"].status.value != "rejected"
    assert intents["JNJ"].status.value != "rejected"
    jnj_reasons = intents["JNJ"].risk_snapshot.get("reasons", [])
    assert not any("SECTOR" in r for r in jnj_reasons)  # never touched by Technology's own exhaustion
    # JNJ's approved quantity should match what an UNCONSTRAINED position of
    # its own would get -- the same order of magnitude as MSFT's own
    # unconstrained approval, not squeezed down like AAPL's.
    assert Decimal(intents["JNJ"].risk_snapshot["approved_quantity"]) > Decimal("0")


@respx.mock
async def test_opportunity_metadata_carries_sector_and_correlation_provenance(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account()
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_spy_bars()
    _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["sector"] == "Technology"
    assert opportunity.metadata["correlation_penalty_applied"] is False  # no other candidates/holdings this cycle to correlate against
    assert opportunity.metadata["max_correlation"] is None


@respx.mock
async def test_highly_correlated_candidate_is_demoted_below_uncorrelated_peer(tmp_path) -> None:
    """Two candidates with near-identical daily-return series (deliberately
    constructed, verified via pearson_correlation directly first) both pass
    every gate individually -- the correlated one, even if it individually
    scored a HIGHER composite, must execute AFTER a genuinely uncorrelated
    peer, proving the demotion actually reorders pass 2 execution, not just
    a metadata annotation."""
    from tradepulse.strategy.correlation import pearson_correlation

    # AAPL and a synthetic near-duplicate of it (scaled 2x, same shape) --
    # verify they really are highly correlated before relying on that below.
    duplicate_closes = [c * 2.0 for c in _STRONGER_BULLISH_CLOSES]  # same shape/returns as MSFT's own fixture, just rescaled
    corr = pearson_correlation(_STRONGER_BULLISH_CLOSES, duplicate_closes)
    assert corr is not None and corr > Decimal("0.99")

    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([
                {"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Duplicate of MSFT's shape."},
                {"symbol": "MSFT", "recommendation": "BUY", "confidence": 90, "summary": "Stronger, uncorrelated setup."},
            ]),
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_quote_for("MSFT")
    _mock_bars_for("AAPL", duplicate_closes)  # highly correlated with MSFT's own series
    _mock_bars_for("MSFT", _STRONGER_BULLISH_CLOSES, _STRONGER_BULLISH_VOLUMES, _STRONGER_BULLISH_BAND)
    _mock_spy_bars()
    call_order = _mock_multi_symbol_dynamic_full_fill()

    summary = await run_scan_cycle(
        repositories, ai_provider, market_data, broker, gateway, UNIVERSE_TWO_EQUITIES, limits, AssetClass.EQUITY, clock=lambda: NOW,
    )
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert call_order == ["MSFT", "AAPL"]  # AAPL demoted below MSFT despite ranking, due to correlation

    opp_rows = await repositories.opportunities.list_all()
    opportunities = {hydrate("opportunities", row["payload"]).asset.symbol: hydrate("opportunities", row["payload"]) for row in opp_rows}
    assert opportunities["AAPL"].metadata["correlation_penalty_applied"] is True
    assert opportunities["MSFT"].metadata["correlation_penalty_applied"] is False


@respx.mock
async def test_candidate_correlated_with_existing_holding_is_demoted(tmp_path) -> None:
    """A candidate highly correlated with an asset ALREADY HELD (not just
    another candidate this cycle) must also be demoted -- proves the
    candidate-vs-holdings fetch path, not just candidate-vs-candidate."""
    from tradepulse.strategy.correlation import pearson_correlation

    duplicate_closes = [c * 2.0 for c in _BULLISH_CLOSES]
    corr = pearson_correlation(_BULLISH_CLOSES, duplicate_closes)
    assert corr is not None and corr > Decimal("0.99")

    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    # An existing MSFT holding, highly correlated (once fetched) with the
    # single AAPL candidate below.
    held_asset = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    holding = Holding(asset=held_asset, quantity=Decimal("5"), average_price=Decimal("300"), updated_at=NOW)
    await repositories.holdings.create_once(asset_identity_key(held_asset), holding)
    _mock_bars_for("MSFT", duplicate_closes)  # the holding's own candle history, fetched by _correlation_adjusted_rank

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account(cash="50000")
    _mock_positions()  # broker reports no open positions -- the correlation check reads local `holdings`, not the broker, for this
    _mock_quote_for("AAPL")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_spy_bars()
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    # Still executes -- correlation demotes rank, never rejects outright --
    # but the persisted metadata proves the penalty was applied.
    assert order_route.call_count == 1
    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["correlation_penalty_applied"] is True


@respx.mock
async def test_candidate_not_demoted_by_correlation_with_a_different_asset_class_holding(tmp_path) -> None:
    """Rev.81 Finding 2a: pearson_correlation tail-aligns purely by array
    position with no date awareness -- crypto trades 365 days/yr against
    equity's ~252, so comparing an equity candidate's closes against a
    crypto holding's closes is economically meaningless even when the raw
    numbers happen to line up. _correlation_adjusted_rank must only compare
    within the same asset class -- an AAPL candidate must NOT be demoted by
    a highly "correlated" (by raw array position only) BTC/USD holding."""
    from tradepulse.strategy.correlation import pearson_correlation

    duplicate_closes = [c * 2.0 for c in _BULLISH_CLOSES]
    corr = pearson_correlation(_BULLISH_CLOSES, duplicate_closes)
    assert corr is not None and corr > Decimal("0.99")  # would demote if asset class were ignored

    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    held_asset = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
    # Small notional (0.01 * 60000 = 600) -- deliberately kept well under
    # max_total_exposure_pct so risk evaluation isn't what blocks the order;
    # this test is about correlation-demotion, not exposure sizing.
    holding = Holding(asset=held_asset, quantity=Decimal("0.01"), average_price=Decimal("60000"), updated_at=NOW)
    await repositories.holdings.create_once(asset_identity_key(held_asset), holding)
    _mock_crypto_bars(duplicate_closes)  # the crypto holding's own candle history

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_spy_bars()
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED
    assert order_route.call_count == 1
    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["correlation_penalty_applied"] is False
    assert opportunity.metadata["max_correlation"] is None  # no same-asset-class comparison existed this cycle


@respx.mock
async def test_holding_candle_fetch_failure_degrades_gracefully(tmp_path) -> None:
    repositories, broker, ai_provider, market_data, gateway, limits = await _setup(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    _mock_market_open()
    held_asset = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    holding = Holding(asset=held_asset, quantity=Decimal("5"), average_price=Decimal("300"), updated_at=NOW)
    await repositories.holdings.create_once(asset_identity_key(held_asset), holding)
    respx.get("https://data.alpaca.markets/v2/stocks/MSFT/bars").mock(return_value=httpx.Response(500, json={"message": "server error"}))

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 90, "summary": "Momentum."}])
        )
    )
    _mock_account(cash="50000")
    _mock_positions()
    _mock_quote_for("AAPL")
    _mock_bars_for("AAPL", _BULLISH_CLOSES)
    _mock_spy_bars()
    order_route = _mock_dynamic_full_fill()

    summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, UNIVERSE, limits, AssetClass.EQUITY, clock=lambda: NOW)
    await broker.aclose()
    await ai_provider.aclose()

    assert summary.status == ScanRunStatus.COMPLETED  # the holding's failed candle fetch never crashes the cycle
    assert order_route.call_count == 1
    opp_rows = await repositories.opportunities.list_all()
    opportunity = hydrate("opportunities", opp_rows[0]["payload"])
    assert opportunity.metadata["correlation_penalty_applied"] is False  # no correlation signal available -- not penalized on missing data


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
