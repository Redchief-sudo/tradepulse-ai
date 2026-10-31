"""Shared Rev.140 runtime fixture: the real gateway and settlement on a
funded SimulatedBroker with the fill cursor bootstrapped. Registered as a
fixture by conftest.py."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from dbae_support import FullBandCapabilities

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest, ExecutionResult
from tradepulse.execution.idempotency import release_symbol_reservation, reserve_symbol_for_execution
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, SessionState, Side, TradingSession
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
from tradepulse.risk import save_session
from tradepulse.runtime_lease import account_lease
from tradepulse.settlement import SettlementProcessor

T0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
KEY = "equity:default:alpaca:AAPL"


@dataclass
class Rt:
    repositories: PersistenceRepositories
    database: AsyncSQLiteDatabase
    broker: SimulatedBroker
    clock: ManualClock
    gateway: ExecutionGateway
    settlement: SettlementProcessor
    alerts: TelegramAlerter
    sent: list[tuple[str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def quote(self, bid: str = "199.50", ask: str = "199.60") -> None:
        self.clock.advance(timedelta(seconds=1))
        self.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(bid), Decimal(ask), at=self.clock())
        self.clock.advance(timedelta(seconds=1))

    async def buy(self, quantity: str = "10") -> ExecutionResult:
        return await self.gateway.execute_intent(ExecutionRequest(
            asset=AAPL, side=Side.BUY, requested_quantity=Decimal(quantity), strategy="test", confidence=Decimal(90),
            stop_loss=Decimal(190), target_price=Decimal(220)))

    async def sell(self, quantity: str, token: str | None, *, decision: str = "exit", reserve: bool = True) -> ExecutionResult:
        if reserve and token is not None:
            assert await reserve_symbol_for_execution(self.database, AAPL, token)
        try:
            return await self.gateway.execute_intent(ExecutionRequest(
                asset=AAPL, side=Side.SELL, requested_quantity=Decimal(quantity), strategy="monitor_exit",
                decision_id=decision, exit_reason="stop_loss", symbol_lock_owner_token=token))
        finally:
            if reserve and token is not None:
                await release_symbol_reservation(self.database, AAPL, token)

    async def position(self) -> Decimal:
        return next((p.qty for p in await self.broker.get_positions() if p.symbol == "AAPL"), Decimal(0))

    async def intents(self) -> list[Any]:
        return [hydrate("trade_intents", row["payload"]) for row in await self.repositories.trade_intents.list_all()]

    async def exits_at_broker(self) -> list[Any]:
        return [o for o in self.broker._orders.values() if o.side == Side.SELL]


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[Rt]:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/gateway.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)),
                             seed=140, opening_journal=True)
    alerts = TelegramAlerter(None, None)
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="rev140-gateway", directory=tmp_path) as lease:
        await bootstrap_fill_cursor(repositories, broker, lease, reason="test", now=clock())
        settlement = SettlementProcessor(repositories, alerts, clock=lambda: clock() - timedelta(milliseconds=1))
        gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker), settlement, alerts,
                                   risk_limits_for_profile("balanced"), ExecutionMode.PAPER,
                                   clock=lambda: clock() - timedelta(milliseconds=1), lease=lease,
                                   capabilities=FullBandCapabilities(broker, lambda: clock() - timedelta(milliseconds=1)))
        state = Rt(repositories, database, broker, clock, gateway, settlement, alerts)

        async def record(severity: str, message: str, details: object = None) -> None:
            state.sent.append((severity, message))

        alerts.send = record  # type: ignore[method-assign]
        for name in ("get_open_orders", "get_positions", "get_activities", "place_order", "get_account"):
            original = getattr(broker, name)

            def spy(*args: Any, _original: Any = original, _name: str = name, **kwargs: Any) -> Any:
                state.calls.append(_name)
                return _original(*args, **kwargs)

            setattr(broker, name, spy)
        await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, T0))
        state.quote()
        yield state
        await broker.aclose()
