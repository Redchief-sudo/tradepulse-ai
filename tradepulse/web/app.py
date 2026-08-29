"""Local operator dashboard backend -- a thin, read-mostly HTTP layer over
the SAME domain code the CLI uses. No business logic lives here: every GET
proxies `PersistenceRepositories`/live broker calls through the existing
`hydrate`/`encode_payload` machinery, and every session-control POST calls
straight into `tradepulse.session_commands` -- the exact functions
`tradepulse start`/`stop`/`reset-risk`/`reset-integrity` call. A dashboard
button and the CLI command it mirrors are never two implementations of the
same decision, only two callers of one.

Local-only by construction: `tradepulse dashboard` (see cli.py) always
binds 127.0.0.1, never configurable to anything else -- there is no
authentication/authorization layer yet, so anything network-reachable here
would be unauthenticated start/stop/reset-risk/reset-integrity authority.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decimal import Decimal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.config import Settings
from tradepulse.models import asset_key_from_broker_symbol
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.providers import AlpacaMarketDataProvider, resolve_market_data_capabilities
from tradepulse.risk import build_portfolio_snapshot, load_session
from tradepulse.session_commands import build_broker, run_reset_integrity, run_reset_risk, run_start, run_stop

_CONFIRMATION_PHRASE = "RESET_FINANCIAL_INTEGRITY"
_ACCOUNT_CACHE_SECONDS = 5


@dataclass
class AppState:
    settings: Settings
    repositories: PersistenceRepositories
    broker: AlpacaClient
    market_data: AlpacaMarketDataProvider
    account_cache: tuple[float, Any] | None = None


async def build_app_state(settings: Settings) -> AppState:
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = build_broker(settings)
    market_data = AlpacaMarketDataProvider(broker)
    return AppState(settings=settings, repositories=repositories, broker=broker, market_data=market_data)


def _json(data: Any, status_code: int = 200) -> Response:
    """Every domain object/list this app returns is already a
    persistence-hydrated dataclass or a plain dict -- reuse the exact same
    JSON-safe encoder the persistence layer already relies on
    (Decimal/datetime/Enum-safe) rather than hand-writing a parallel
    Pydantic schema per resource."""
    return Response(content=encode_payload(data), media_type="application/json", status_code=status_code)


def _state(request: Request) -> AppState:
    return request.app.state.tp


class ResetIntegrityRequest(BaseModel):
    force: bool = False
    confirmation: str | None = None


def create_app(state: AppState, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="TradePulse Dashboard")
    app.state.tp = state

    # ---- Session control -------------------------------------------------

    @app.get("/api/session")
    async def get_session(request: Request) -> Response:
        session = await load_session(_state(request).repositories)
        return _json(session)

    @app.post("/api/session/start")
    async def post_start(request: Request) -> Response:
        s = _state(request)
        exit_code = await run_start(s.settings)
        session = await load_session(s.repositories)
        return _json({"exit_code": exit_code, "session": session})

    @app.post("/api/session/stop")
    async def post_stop(request: Request) -> Response:
        s = _state(request)
        exit_code = await run_stop(s.settings)
        session = await load_session(s.repositories)
        return _json({"exit_code": exit_code, "session": session})

    @app.post("/api/session/reset-risk")
    async def post_reset_risk(request: Request) -> Response:
        s = _state(request)
        exit_code = await run_reset_risk(s.settings)
        session = await load_session(s.repositories)
        return _json({"exit_code": exit_code, "session": session})

    @app.post("/api/session/reset-integrity")
    async def post_reset_integrity(request: Request, body: ResetIntegrityRequest) -> Response:
        # Operator-interface guard ON TOP OF (never instead of) the CLI's
        # own --force semantics -- a stray click or a bare `force: true`
        # must never be enough to trigger an unverified critical override.
        # This never touches session_commands or the audit trail itself;
        # it only decides whether the request is even allowed to reach it.
        if body.force and body.confirmation != _CONFIRMATION_PHRASE:
            raise HTTPException(status_code=400, detail=f"force=true requires confirmation == {_CONFIRMATION_PHRASE!r}")
        s = _state(request)
        exit_code = await run_reset_integrity(s.settings, force=body.force)
        session = await load_session(s.repositories)
        return _json({"exit_code": exit_code, "session": session})

    # ---- Live broker reads -------------------------------------------------

    @app.get("/api/account")
    async def get_account(request: Request) -> Response:
        s = _state(request)
        now = time.monotonic()
        if s.account_cache is not None and now - s.account_cache[0] < _ACCOUNT_CACHE_SECONDS:
            return _json(s.account_cache[1])
        try:
            account = await s.broker.get_account()
        except (AlpacaError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=f"BROKER_UNAVAILABLE: {exc}") from exc
        s.account_cache = (now, account)
        return _json(account)

    @app.get("/api/positions")
    async def get_positions(request: Request) -> Response:
        s = _state(request)
        try:
            positions = await s.broker.get_positions()
        except (AlpacaError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=f"BROKER_UNAVAILABLE: {exc}") from exc
        enriched = []
        for position in positions:
            holding_row = await s.repositories.holdings.get(asset_key_from_broker_symbol(position.asset_class, position.symbol))
            holding = hydrate("holdings", holding_row["payload"]) if holding_row is not None else None
            enriched.append({
                "position": position,
                "stop_loss": holding.stop_loss if holding is not None else None,
                "target_price": holding.target_price if holding is not None else None,
            })
        return _json(enriched)

    @app.get("/api/risk-exposure")
    async def get_risk_exposure(request: Request) -> Response:
        s = _state(request)
        try:
            account = await s.broker.get_account()
        except (AlpacaError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=f"BROKER_UNAVAILABLE: {exc}") from exc
        snapshot = await build_portfolio_snapshot(
            s.repositories, cash_balance=account.cash, account_equity=account.equity,
            broker_prev_close_equity=account.last_equity,
        )
        return _json(snapshot)

    @app.get("/api/pnl")
    async def get_pnl(request: Request, limit: int = 50) -> Response:
        s = _state(request)
        pnl_rows = await s.repositories.pnl_records.list_recent(limit)
        realized = [hydrate("pnl_records", row["payload"]) for row in pnl_rows]
        try:
            positions = await s.broker.get_positions()
        except (AlpacaError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=f"BROKER_UNAVAILABLE: {exc}") from exc
        unrealized_total = sum((p.unrealized_pl for p in positions), Decimal("0"))
        return _json({"realized": realized, "unrealized_total": unrealized_total, "positions_unrealized": positions})

    # ---- Market-data capability ------------------------------------------

    @app.get("/api/market-data-capability")
    async def get_market_data_capability(request: Request) -> Response:
        """Reads from the most recent ScanRun per lane -- what the trading
        invocation actually used, never a fresh probe against Alpaca (which
        could show something no scan cycle has actually run with yet)."""
        s = _state(request)
        rows = await s.repositories.scan_runs.list_recent(200)
        by_lane: dict[str, Any] = {}
        for row in rows:
            scan_run = hydrate("scan_runs", row["payload"])
            key = scan_run.asset_class.value
            if key in by_lane or scan_run.market_data_tier is None:
                continue
            by_lane[key] = scan_run
        return _json(by_lane)

    @app.post("/api/market-data-capability/probe")
    async def post_market_data_capability_probe(request: Request) -> Response:
        """An explicit, on-demand LIVE probe -- distinct from the GET above,
        which only ever reports what a real scan cycle already used."""
        s = _state(request)
        capabilities = await resolve_market_data_capabilities(s.broker, s.settings.alpaca_market_data_tier)
        return _json({
            "tier_label": capabilities.tier_label, "equity_feed": capabilities.equity_feed,
            "option_feed": capabilities.option_feed,
        })

    @app.get("/api/rate-limit")
    async def get_rate_limit(request: Request) -> Response:
        """The most recently observed Alpaca X-RateLimit-* snapshot from
        real request traffic (see AlpacaClient.rate_limit_snapshot) --
        opportunistic telemetry, not a live probe. null until at least one
        request has actually carried the headers."""
        s = _state(request)
        return _json(s.broker.rate_limit_snapshot)

    # ---- Recent-activity reads --------------------------------------------

    def _recent_route(table: str, path: str) -> None:
        async def handler(request: Request, limit: int = 50) -> Response:
            rows = await getattr(_state(request).repositories, table).list_recent(limit)
            return _json([hydrate(table, row["payload"]) for row in rows])

        app.get(path)(handler)

    _recent_route("opportunities", "/api/opportunities")
    _recent_route("fills", "/api/fills")
    _recent_route("settlements", "/api/settlements")
    _recent_route("reconciliation_records", "/api/reconciliation")
    _recent_route("audit_events", "/api/audit-events")
    _recent_route("scan_runs", "/api/scan-runs")

    @app.get("/api/trade-intents")
    async def get_trade_intents(request: Request, status: str | None = None, limit: int = 50) -> Response:
        s = _state(request)
        if status is not None:
            rows = await s.repositories.trade_intents.list_by_status(status, limit)
        else:
            rows = await s.repositories.trade_intents.list_recent(limit)
        return _json([hydrate("trade_intents", row["payload"]) for row in rows])

    if frontend_dist is not None and frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app
