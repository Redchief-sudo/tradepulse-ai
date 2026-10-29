"""Rev.142: adoption on the real runtime (T2-T4, T7-T10, T12, T14, T19, T22, T25, T29)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradepulse.adoption import (
    AdoptionConfig,
    AdoptionGateError,
    adoption_pass,
    adoptions,
    reentry_excluded,
    startup_gate,
)
from tradepulse.adoption.accounting import adoption_attribution_rows
from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import Fault, ManualClock, SimSession, SimulatedBroker
from tradepulse.broker.types import AlpacaOrderRequest
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, SessionState, Side, TradingSession
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
from tradepulse.risk import save_session
from tradepulse.runtime_lease import account_lease
from tradepulse.settlement import SettlementProcessor
from tradepulse.settlement.accounting import accounting_issues

T0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Monday 10:00 NY
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
KEY = "equity:default:alpaca:AAPL"
TABLES = ("fills", "cash_ledger", "trade_intents", "settlements")


@dataclass
class Rig:
    repositories: PersistenceRepositories
    broker: SimulatedBroker
    clock: ManualClock
    gateway: ExecutionGateway
    settlement: SettlementProcessor
    alerts: TelegramAlerter
    config: AdoptionConfig

    def quote(self, bid: str, ask: str) -> None:
        self.clock.advance(timedelta(seconds=1))
        self.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(bid), Decimal(ask), at=self.clock())
        self.clock.advance(timedelta(seconds=1))

    async def monitor(self) -> Any:
        return await run_position_monitor(self.repositories, self.broker, AlpacaMarketDataProvider(self.broker), self.gateway,
                                          self.alerts, self.config.risk_limits, clock=lambda: self.clock(), adoption=self.config)

    async def counts(self) -> dict[str, int]:
        return {t: len(await paginate_all_rows(getattr(self.repositories, t))) for t in TABLES}

    async def lot(self) -> Any:
        return next(hydrate("position_lots", r["payload"]) for r in await paginate_all_rows(self.repositories.position_lots))


async def _external(broker: SimulatedBroker, clock: ManualClock, side: Side, qty: str, coid: str) -> None:
    await broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(qty), side=side, client_order_id=coid))
    clock.advance(timedelta(seconds=2))
    await broker.get_positions()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/adopt.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)),
                             seed=142, opening_journal=True)
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("99.90"), Decimal(100), at=clock())
    clock.advance(timedelta(seconds=1))
    await _external(broker, clock, Side.BUY, "10", "bought-outside")  # before the cursor: explained by its baseline
    alerts = TelegramAlerter(None, None)
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="rev142", directory=tmp_path) as lease:
        await bootstrap_fill_cursor(repositories, broker, lease, reason="test", now=clock())
        settlement = SettlementProcessor(repositories, alerts, clock=lambda: clock() - timedelta(milliseconds=1))
        limits = risk_limits_for_profile("balanced")
        gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker), settlement, alerts, limits,
                                   ExecutionMode.PAPER, clock=lambda: clock() - timedelta(milliseconds=1), lease=lease)
        await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, T0))
        yield Rig(repositories, broker, clock, gateway, settlement, alerts,
                  AdoptionConfig(lease.ref.key, limits, AlpacaMarketDataProvider(broker)))
        await broker.aclose()


async def test_gate_passes_and_refuses_without_the_cursor(rig: Rig, tmp_path: Path) -> None:
    """T22: the gate's three operational checks, in this process."""
    await startup_gate(rig.repositories, rig.gateway, rig.config.account_ref)
    with pytest.raises(AdoptionGateError, match="fill_cursor"):
        await startup_gate(rig.repositories, rig.gateway, "alpaca:OTHER")


async def test_stability_then_adoption_neutral_and_idempotent(rig: Rig) -> None:
    """T7, T2, T3, T4: an unexpected position is adopted on its second
    matching sighting, from the broker's average entry, writing no fill,
    cash, intent or settlement; repeating changes nothing."""
    before = await rig.counts()
    assert await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock()) == []
    [adopted] = await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    assert await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock()) == []
    assert await rig.counts() == before
    row = (await rig.repositories.position_adoptions.database.run(adoptions))[adopted]
    assert (row["classification"], row["adopted_quantity"], row["broker_avg_entry_price"], row["stop_source"]) == (
        "unexpected_external", "10", "100", "pct_fallback")  # the simulator serves no candles
    lot = await rig.lot()
    assert (lot.origin, lot.adoption_id, lot.originating_fill_id) == ("adopted", adopted, f"adoption:{adopted}")
    holding = hydrate("holdings", (await rig.repositories.holdings.get(KEY))["payload"])
    assert (holding.quantity, holding.stop_loss, holding.target_price, holding.current_stop) == (Decimal(10), Decimal(row["stop_loss"]), None, None)
    assert "account_number" not in row["broker_observation"]


async def test_a_quantity_change_resets_the_episode(rig: Rig) -> None:
    """T7."""
    await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    await _external(rig.broker, rig.clock, Side.BUY, "1", "more")
    assert await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock()) == []
    assert len(await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())) == 1


async def test_concurrent_cycles_adopt_once(rig: Rig) -> None:
    """T5."""
    await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    positions = await rig.broker.get_positions()
    results = await asyncio.gather(*(adoption_pass(rig.repositories, positions, rig.config, rig.alerts, rig.clock()) for _ in range(3)))
    assert sum(len(r) for r in results) == 1


async def test_breached_stop_sells_in_the_adoption_cycle(rig: Rig) -> None:
    """T8, T14, T25: adopted below its stop, sold the same cycle through
    Rev.140's gate; settled exactly once; accounting verifies; P&L split."""
    for _ in range(2):
        await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    rig.quote("80", "80.10")
    summary = await rig.monitor()
    assert summary.exits_triggered == 1
    await rig.settlement.process_pending(force_retry=True)
    lot = await rig.lot()
    assert (lot.remaining_quantity, len(lot.closures)) == (Decimal(0), 1)
    assert await rig.repositories.holdings.get(KEY) is None
    assert await accounting_issues(rig.repositories) == {}
    await rig.settlement.process_pending(force_retry=True)  # a replay changes nothing
    assert len((await rig.lot()).closures) == 1
    [attribution] = [hydrate("trade_attributions", r["payload"]) for r in await paginate_all_rows(rig.repositories.trade_attributions)]
    assert (attribution.origin, attribution.opening_trade_intent_id) == ("adopted", lot.originating_fill_id)
    from tradepulse.adoption.accounting import recompute_attributions

    await rig.repositories.position_adoptions.database.run(lambda c: recompute_attributions(c, rig.clock()), write=True)
    [row] = await rig.repositories.position_adoptions.database.run(adoption_attribution_rows)
    adoption = next(iter((await rig.repositories.position_adoptions.database.run(adoptions)).values()))
    difference = Decimal(row["broker_basis_pnl"]) - Decimal(row["managed_pnl"])
    assert difference == (Decimal(adoption["adoption_mark_price"]) - Decimal(adoption["broker_avg_entry_price"])) * Decimal(row["qty"])


async def test_breached_outside_market_hours_is_deferred(rig: Rig) -> None:
    """T8: closed market -> deferred, with the deferral alert."""
    for _ in range(2):
        await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    rig.clock.advance(timedelta(hours=8))  # 18:00 NY
    rig.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(80), Decimal("80.10"), at=rig.clock())
    summary = await rig.monitor()
    assert (summary.exits_triggered, summary.deferred_exits) == (0, 1)


async def test_short_and_partially_managed_are_not_adopted(rig: Rig) -> None:
    """T9: a short position is never adopted."""
    rig.broker.inject(Fault.ACCEPT_OVERSELL)
    await _external(rig.broker, rig.clock, Side.SELL, "12", "oversold")
    for _ in range(2):
        assert await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock()) == []


async def test_exit_refused_by_rev140_keeps_the_adoption(rig: Rig) -> None:
    """T10: an external open sell order makes Rev.140 refuse; the adoption and
    holding stay; ADOPTED_EXIT_REFUSED once per hour."""
    for _ in range(2):
        await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    rig.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(80), Decimal("80.10"), at=rig.clock())
    await rig.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(10), side=Side.SELL, client_order_id="outside-limit",
                                                    order_type="limit", limit_price=Decimal(500)))
    rig.clock.advance(timedelta(seconds=2))
    for _ in range(2):
        await rig.monitor()
    alerts = [r for r in await paginate_all_rows(rig.repositories.audit_events) if r["payload"]["event_type"] == "adopted_exit_refused"]
    assert len(alerts) == 1
    assert await rig.repositories.holdings.get(KEY) is not None


async def test_reentry_blocked_after_adoption(rig: Rig) -> None:
    """T29, T19: no strategy BUY of an adopted asset outside a generation, at
    the gateway, even after full closure."""
    for _ in range(2):  # an unexpected position is adopted on its second matching sighting
        await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    result = await rig.gateway.execute_intent(ExecutionRequest(
        asset=AAPL, side=Side.BUY, requested_quantity=Decimal(1), strategy="test", confidence=Decimal(90),
        stop_loss=Decimal(90), target_price=Decimal(120)))
    assert (result.status, result.reasons) == ("rejected", ["ADOPTED_ASSET_REENTRY_BLOCKED"])
    assert await rig.repositories.position_adoptions.database.run(lambda c: reentry_excluded(c, None, KEY))


async def test_setting_off_changes_nothing(rig: Rig) -> None:
    """T22 (off): no adoption, the position stays reported as unmanaged, exactly as before."""
    summary = await run_position_monitor(rig.repositories, rig.broker, AlpacaMarketDataProvider(rig.broker), rig.gateway, rig.alerts,
                                         rig.config.risk_limits, clock=lambda: rig.clock())
    assert summary.exits_triggered == 0
    assert await paginate_all_rows(rig.repositories.position_lots) == []
    assert [r["payload"]["event_type"] for r in await paginate_all_rows(rig.repositories.audit_events)] == ["unmanaged_broker_position"]
