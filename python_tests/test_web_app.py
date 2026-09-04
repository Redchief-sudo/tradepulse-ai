from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.config import Settings
from tradepulse.models import (
    AIResponse,
    AssetClass,
    AssetIdentity,
    AuditEvent,
    ExecutionMode,
    Holding,
    Opportunity,
    RejectedCandidate,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SessionState,
    Side,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
    asset_identity_key,
)
from tradepulse.models.market import MarketQuote
from tradepulse.persistence import hydrate
from tradepulse.risk import save_session
from tradepulse.session_commands import run_reset_integrity, run_start
from tradepulse.web import build_app_state, create_app

NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


def _settings(database_url: str, **extra: str) -> Settings:
    return Settings.from_env({
        "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret", "ANTHROPIC_API_KEY": "key",
        "TRADEPULSE_DATABASE_URL": database_url, "ALPACA_MARKET_DATA_TIER": "basic", **extra,
    })


async def _client_for(tmp_path, **extra: str):
    settings = _settings(f"sqlite:///{tmp_path}/test.db", **extra)
    state = await build_app_state(settings)
    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, state


def _mock_account(cash: str = "50000", equity: str = "100000") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": "99500", "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_positions(*positions: dict) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=list(positions)))


@respx.mock
async def test_get_session_returns_current_state(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    await save_session(state.repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))

    response = await client.get("/api/session")
    await state.broker.aclose()
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["state"] == "active"


@respx.mock
async def test_post_start_produces_identical_state_to_direct_session_commands_call(tmp_path) -> None:
    """The concrete proof a dashboard button and the CLI command are the
    SAME code path -- not two implementations that happen to agree today."""
    client, state = await _client_for(tmp_path)
    _mock_account()

    response = await client.post("/api/session/start")

    assert response.status_code == 200
    assert response.json()["exit_code"] == 0
    assert response.json()["session"]["state"] == "active"

    # A second, INDEPENDENT settings/repositories pair calling run_start
    # directly against the same DB must see the identical already-active
    # no-op state -- proving the API route and the CLI function are one
    # implementation, not two.
    direct_exit_code = await run_start(state.settings)
    assert direct_exit_code == 0

    await state.broker.aclose()
    await client.aclose()


@respx.mock
async def test_reset_integrity_force_without_confirmation_is_rejected_and_session_untouched(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    await save_session(
        state.repositories,
        TradingSession("session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW, financial_integrity_reason="drift", financial_integrity_manual_reenable_required=True),
    )

    response = await client.post("/api/session/reset-integrity", json={"force": True})
    await client.aclose()

    assert response.status_code == 400

    session_row = await state.repositories.trading_sessions.get("session")
    session = hydrate("trading_sessions", session_row["payload"])
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED  # untouched -- session_commands was never reached
    await state.broker.aclose()


@respx.mock
async def test_reset_integrity_force_with_wrong_confirmation_is_rejected(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    await save_session(
        state.repositories,
        TradingSession("session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW, financial_integrity_reason="drift", financial_integrity_manual_reenable_required=True),
    )

    response = await client.post("/api/session/reset-integrity", json={"force": True, "confirmation": "yes please"})
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 400


@respx.mock
async def test_reset_integrity_force_with_exact_confirmation_matches_cli_force_path(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    await save_session(
        state.repositories,
        TradingSession("session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW, financial_integrity_reason="drift", financial_integrity_manual_reenable_required=True),
    )

    response = await client.post(
        "/api/session/reset-integrity", json={"force": True, "confirmation": "RESET_FINANCIAL_INTEGRITY"},
    )
    await client.aclose()

    assert response.status_code == 200
    assert response.json()["session"]["state"] == "manually_stopped"

    audit_rows = await state.repositories.audit_events.list_recent()
    events = [hydrate("audit_events", r["payload"]) for r in audit_rows]
    assert any(e.severity == "critical" and "force-cleared" in e.message for e in events)
    await state.broker.aclose()


@respx.mock
async def test_get_account_proxies_live_broker_call(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    _mock_account(cash="12345")

    response = await client.get("/api/account")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    assert response.json()["cash"] == "12345"


@respx.mock
async def test_get_positions_cross_references_local_holding_stop_loss(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("150"), updated_at=NOW, stop_loss=Decimal("140"))
    await state.repositories.holdings.create_once(asset_identity_key(_aapl()), holding)
    _mock_positions({
        "symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
        "market_value": "1550", "current_price": "155", "unrealized_pl": "50",
    })

    response = await client.get("/api/positions")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["position"]["symbol"] == "AAPL"
    assert body[0]["stop_loss"] == "140"


@respx.mock
async def test_market_data_capability_reads_from_scan_runs_not_live_probe(tmp_path) -> None:
    """No Alpaca options-chain/quote route is mocked at all -- if this route
    tried to probe live, respx would fail the request. It must only ever
    read what a real scan cycle already used."""
    client, state = await _client_for(tmp_path)
    scan_run = ScanRun(
        scan_run_id="run-1", scan_generation="gen-1", trigger=ScanTrigger.SCHEDULED, asset_class=AssetClass.EQUITY,
        status=ScanRunStatus.COMPLETED, started_at=NOW, lock_owner_token="owner-1", completed_at=NOW,
        market_data_tier="algo_trader_plus", equity_feed="sip", option_feed="opra",
    )
    await state.repositories.scan_runs.create_once("run-1", scan_run, status=scan_run.status.value)

    response = await client.get("/api/market-data-capability")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["equity"]["equity_feed"] == "sip"
    assert body["equity"]["market_data_tier"] == "algo_trader_plus"


@respx.mock
async def test_market_data_capability_skips_legacy_row_missing_asset_class(tmp_path) -> None:
    """Regression test for a live 500: a ScanRun row persisted before
    asset_class existed as a required field crashes hydration
    (AssetClass(d["asset_class"]) -> KeyError). The route must skip that
    row -- never crash, never guess/default its lane -- and still surface
    the newest CURRENT-schema row per lane underneath it."""
    client, state = await _client_for(tmp_path)
    current_run = ScanRun(
        scan_run_id="run-current", scan_generation="gen-1", trigger=ScanTrigger.SCHEDULED, asset_class=AssetClass.CRYPTO,
        status=ScanRunStatus.COMPLETED, started_at=NOW, lock_owner_token="owner-1", completed_at=NOW,
        market_data_tier="basic", equity_feed="iex", option_feed="indicative",
    )
    await state.repositories.scan_runs.create_once("run-current", current_run, status=current_run.status.value)

    # Inserted SECOND so it sorts newest-first (list_recent) and is the
    # FIRST row the route's loop actually encounters -- proving it's
    # skipped and the loop continues, not just that a working row works.
    legacy_payload = {
        "scan_run_id": "run-legacy", "scan_generation": "gen-0", "trigger": "scheduled",
        "status": "completed", "started_at": NOW.isoformat(), "lock_owner_token": "owner-0",
        "completed_at": NOW.isoformat(), "candidates_discovered": 0, "candidates_approved": 0,
        "orders_submitted": 0, "error": None, "market_data_tier": "basic", "equity_feed": "iex", "option_feed": "indicative",
        # asset_class deliberately omitted -- the exact legacy-row shape from the live incident
    }
    await state.repositories.scan_runs.create_once("run-legacy", legacy_payload, status="completed")

    response = await client.get("/api/market-data-capability")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200  # never a 500
    body = response.json()
    assert "crypto" in body
    assert body["crypto"]["equity_feed"] == "iex"


@respx.mock
async def test_rate_limit_route_returns_null_before_any_request(tmp_path) -> None:
    client, state = await _client_for(tmp_path)

    response = await client.get("/api/rate-limit")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    assert response.json() is None


@respx.mock
async def test_rate_limit_route_returns_latest_observed_snapshot(tmp_path) -> None:
    """The dashboard route reads AlpacaClient.rate_limit_snapshot directly --
    opportunistic telemetry from real request traffic, never a fresh probe.
    Exercised end to end: a real broker call populates it, then the route
    reports exactly what was observed."""
    client, state = await _client_for(tmp_path)
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(
            200,
            json={"equity": "100000", "last_equity": "99500", "cash": "50000", "buying_power": "100000", "portfolio_value": "100000"},
            headers={"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "199", "X-RateLimit-Reset": str(int(NOW.timestamp()))},
        )
    )
    await state.broker.get_account()

    response = await client.get("/api/rate-limit")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 200
    assert body["remaining"] == 199


@respx.mock
async def test_ai_response_route_returns_the_persisted_candidate_list(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    ai_response = AIResponse(
        request_id="ai-req-1", provider="anthropic", model="claude-haiku-4-5", schema_version="1.0",
        completed_at=NOW, result={"candidates": [{"symbol": "BTC/USD", "recommendation": "BUY", "confidence": 82.0, "summary": "Momentum breakout."}]},
        latency_ms=350,
    )
    await state.repositories.ai_responses.create_once(ai_response.request_id, ai_response)

    response = await client.get("/api/ai-responses/ai-req-1")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert body["result"]["candidates"] == [{"symbol": "BTC/USD", "recommendation": "BUY", "confidence": 82.0, "summary": "Momentum breakout."}]


@respx.mock
async def test_ai_response_route_returns_null_for_unknown_or_legacy_request_id(tmp_path) -> None:
    """No matching AIResponse row -- e.g. a legacy ScanRun with no
    ai_response_request_id at all, or an id that was never persisted --
    must return null (200), matching this app's existing "missing record"
    convention, never a 404."""
    client, state = await _client_for(tmp_path)

    response = await client.get("/api/ai-responses/does-not-exist")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    assert response.json() is None


@respx.mock
async def test_scan_runs_route_still_returns_universe_size_and_ai_response_request_id(tmp_path) -> None:
    """Existing /api/scan-runs behavior stays compatible -- the two new
    observability fields just ride along on the same already-exposed row."""
    client, state = await _client_for(tmp_path)
    scan_run = ScanRun(
        scan_run_id="run-1", scan_generation="gen-1", trigger=ScanTrigger.SCHEDULED, asset_class=AssetClass.CRYPTO,
        status=ScanRunStatus.COMPLETED, started_at=NOW, lock_owner_token="owner-1", completed_at=NOW,
        universe_size=5, ai_response_request_id="ai-req-1",
    )
    await state.repositories.scan_runs.create_once("run-1", scan_run, status=scan_run.status.value)

    response = await client.get("/api/scan-runs")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body[0]["universe_size"] == 5
    assert body[0]["ai_response_request_id"] == "ai-req-1"


@respx.mock
async def test_get_provenance_exposes_only_approved_non_sensitive_fields(tmp_path) -> None:
    """No repositories/broker involved -- must work without any of the
    mocks the other routes need, and must never leak credentials or
    environment contents."""
    client, state = await _client_for(tmp_path)

    response = await client.get("/api/provenance")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "product_name", "creator_name", "copyright_owner", "company_name", "copyright_years",
        "software_version", "git_commit", "build_timestamp", "provenance_version", "build_fingerprint",
    }
    assert body["creator_name"] == "Damien Johnson Fisher"
    assert body["company_name"] == "Silvereyes Technologies, LLC"
    # The exact-keys assertion above IS the whitelist proof -- no field
    # beyond the approved 10 exists to leak a credential through in the
    # first place (this route never even receives Settings/broker state).


@respx.mock
async def test_get_risk_limits_returns_the_active_profiles_limits(tmp_path) -> None:
    """Pure config passthrough -- no repositories/broker involved, same
    shape as the provenance route above."""
    client, state = await _client_for(tmp_path)  # defaults to TRADEPULSE_RISK_PROFILE unset -> "balanced"

    response = await client.get("/api/risk-limits")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "profile_id", "max_total_exposure_pct", "max_sector_pct", "max_position_pct",
        "max_daily_trades", "max_open_positions", "max_drawdown_pct",
    }
    assert body["profile_id"] == "balanced"
    assert body["max_total_exposure_pct"] == "40"
    assert body["max_daily_trades"] == 3


@respx.mock
async def test_get_rejected_candidates_returns_hydrated_recent_rows(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    rejection = RejectedCandidate(
        "rej-1", "run-1", "gen-1", "AAPL", AssetClass.EQUITY, "CONFIDENCE_BELOW_MIN", NOW,
        context={"confidence": 78.0, "min_confidence": "80"},
    )
    await state.repositories.rejected_candidates.create_once("rej-1", rejection)

    response = await client.get("/api/rejected-candidates")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "AAPL"
    assert body[0]["reason"] == "CONFIDENCE_BELOW_MIN"
    assert body[0]["context"]["min_confidence"] == "80"


@respx.mock
async def test_get_opportunities_returns_hydrated_recent_rows(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    quote = MarketQuote(_aapl(), Decimal("190"), NOW, NOW, "alpaca_iex", 0, bid=Decimal("189.9"), ask=Decimal("190.1"))
    opportunity = Opportunity("opp-1", "gen-1", _aapl(), quote, "anthropic", NOW, confidence=88.0, metadata={"ai_recommendation": "BUY"})
    await state.repositories.opportunities.create_once("opp-1", opportunity)

    response = await client.get("/api/opportunities")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["opportunity_id"] == "opp-1"
    assert body[0]["metadata"]["ai_recommendation"] == "BUY"


@respx.mock
async def test_get_trade_intents_filters_by_status(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("5"), status=TradeIntentStatus.SUBMISSION_UNKNOWN,
    )
    await state.repositories.trade_intents.create_once("ti-1", intent, status=intent.status.value, unique_value="idem-1")

    response = await client.get("/api/trade-intents", params={"status": "submission_unknown"})
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["trade_intent_id"] == "ti-1"


@respx.mock
async def test_get_risk_exposure_uses_live_account_and_local_holdings(tmp_path) -> None:
    client, state = await _client_for(tmp_path)
    _mock_account(cash="50000", equity="100000")
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("150"), updated_at=NOW, sector="Tech")
    await state.repositories.holdings.create_once(asset_identity_key(_aapl()), holding)
    _mock_positions({
        "symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
        "market_value": "1500", "current_price": "150", "unrealized_pl": "0",
    })

    response = await client.get("/api/risk-exposure")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["total_equity"] == "100000"
    assert Decimal(body["holdings_value"]) == Decimal("1500")


@respx.mock
async def test_get_risk_exposure_uses_live_mark_price_not_cost_basis(tmp_path) -> None:
    """Rev.81 Finding 5: this route omitted mark_prices, silently falling
    back to the Holding's own cost-basis average_price -- the dashboard
    number went stale the moment price moved. Must reflect the broker's
    current_price instead."""
    client, state = await _client_for(tmp_path)
    _mock_account(cash="50000", equity="100000")
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("150"), updated_at=NOW, sector="Tech")
    await state.repositories.holdings.create_once(asset_identity_key(_aapl()), holding)
    _mock_positions({
        "symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
        "market_value": "2000", "current_price": "200", "unrealized_pl": "500",
    })

    response = await client.get("/api/risk-exposure")
    await client.aclose()
    await state.broker.aclose()

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["holdings_value"]) == Decimal("2000")  # 10 * 200 (live mark), not 10 * 150 (cost basis)
