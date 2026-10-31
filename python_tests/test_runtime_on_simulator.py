"""The whole runtime against the simulated broker (Phase A0 Task 9, Rev.138).

The real ExecutionGateway, SettlementProcessor, position monitor and
reconciliation run unchanged against a SimulatedBroker with Alpaca's
observed fee shape, through the order flow a trading day produces: entry,
settlement, a stop exit, the fee batch posting after the close, and
reconciliation of all of it. The runtime's clock trails the broker's by a
millisecond: the broker stamps an order after the runtime records its
intent, and the runtime reads fills strictly after that record (Alpaca's
``after`` is exclusive).
"""
from __future__ import annotations

import decimal
import json
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from dbae_support import FullBandCapabilities

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import (
    Fault,
    FeePosting,
    FeeRates,
    FeeScenario,
    ManualClock,
    SimSession,
    SimulatedBroker,
)
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest, ExecutionResult
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    SessionState,
    Side,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.monitor import MonitorCycleSummary, run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation import ReconciliationSummary, run_reconciliation
from tradepulse.risk import save_session
from tradepulse.runtime_lease import account_lease
from tradepulse.settlement import SettlementProcessor

RATES = FeeRates(
    reg_per_dollar=Decimal("0.0000206"), taf_per_share=Decimal("0.000166"), taf_max_per_trade=Decimal("8.30"),
    opt_taf_per_contract=Decimal("0.00279"), cat_per_share=Decimal("0.000035"), orf_per_contract=Decimal("0.02295"),
    occ_per_contract=Decimal("0.025"), crypto_rate=Decimal("0.0025"), rounding=decimal.ROUND_UP,
)
POSTING = FeePosting(equity_batch_ny=time(20, 16), option_batch_ny=time(16, 31), occ_delay=timedelta(seconds=12),
                     cfee_asset_delay=timedelta(seconds=3), cfee_usd_delay=timedelta(hours=4, minutes=17))
T0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York
CLOSE = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
AFTER_FEES = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)  # the equity fee batch posts at 20:16 New York
NEXT_OPEN = datetime(2026, 10, 20, 13, 30, tzinfo=UTC)
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


@dataclass
class Runtime:
    repositories: PersistenceRepositories
    database: AsyncSQLiteDatabase
    broker: SimulatedBroker
    clock: ManualClock
    gateway: ExecutionGateway
    settlement: SettlementProcessor
    market_data: AlpacaMarketDataProvider
    alerts: TelegramAlerter
    sent: list[tuple[str, str]] = field(default_factory=list)

    def runtime_clock(self) -> datetime:
        return self.clock() - timedelta(milliseconds=1)

    def quote(self, bid: str, ask: str) -> None:
        self.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(bid), Decimal(ask), at=self.clock())

    def advance_to(self, at: datetime) -> None:
        self.clock.set(at)

    async def buy(self, quantity: str = "5", stop: str = "190") -> ExecutionResult:
        return await self.gateway.execute_intent(ExecutionRequest(
            asset=AAPL, side=Side.BUY, requested_quantity=Decimal(quantity), strategy="test",
            confidence=Decimal("90"), stop_loss=Decimal(stop), target_price=Decimal("220")))

    async def monitor(self) -> MonitorCycleSummary:
        return await run_position_monitor(self.repositories, self.broker, self.market_data, self.gateway, self.alerts,
                                          risk_limits_for_profile("balanced"), clock=self.runtime_clock)

    async def reconcile(self) -> ReconciliationSummary:
        return await run_reconciliation(self.repositories, self.broker, self.settlement, self.alerts,
                                        clock=self.runtime_clock)

    async def rows(self, table: str) -> list[dict[str, object]]:
        def read(connection: object) -> list[dict[str, object]]:
            query = f"SELECT payload FROM {table} ORDER BY rowid"
            return [json.loads(row["payload"]) for row in connection.execute(query)]  # type: ignore[attr-defined]
        return await self.database.run(read)

    async def unresolved(self) -> list[dict[str, object]]:
        """Subjects whose latest reconciliation record is neither matched nor
        corrected -- the soak evidence's reconciliation_issues rule
        (verification/evidence.py). An epoch waiting for its fees records
        drift_detected; that is resolved when a later transition matches."""
        latest: dict[tuple[str, str], dict[str, object]] = {}
        for row in await self.rows("reconciliation_records"):
            kind = "position" if str(row["reconciliation_type"]).startswith("position") else str(row["reconciliation_type"])
            key = (kind, str(row["subject_id"]))
            if key not in latest or str(row["occurred_at"]) >= str(latest[key]["occurred_at"]):
                latest[key] = row
        return [row for row in latest.values() if row["outcome"] not in ("matched", "corrected")]

    async def fee_ledger_entries(self) -> Counter[str]:
        return Counter(str(row["entry_id"]).removeprefix("broker:fee:") for row in await self.rows("cash_ledger")
                       if str(row["entry_id"]).startswith("broker:fee:"))

    async def fee_ids(self) -> list[str]:
        return [a.activity_id for a in await self.broker.get_activities(activity_type="FEE")]

    async def drift_records(self) -> list[dict[str, object]]:
        return [row for row in await self.rows("reconciliation_records") if row["outcome"] == "drift_detected"]

    async def intents(self) -> list[object]:
        return [hydrate("trade_intents", row["payload"]) for row in await self.repositories.trade_intents.list_all()]

    def critical(self) -> list[str]:
        return [message for severity, message in self.sent if severity == "critical"]


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/runtime.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    clock = ManualClock(T0)
    broker = SimulatedBroker(
        clock=clock, session=SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)),
        fee_scenario=FeeScenario.observed_alpaca_2026(RATES, POSTING), seed=11, opening_journal=True,
    )
    alerts = TelegramAlerter(None, None)
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="runtime-on-simulator", directory=tmp_path) as lease:
        # Rev.140: the operator bootstraps the fill cursor before the first run.
        from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
        await bootstrap_fill_cursor(repositories, broker, lease, reason="runtime on simulator", now=clock())
        sent: list[tuple[str, str]] = []

        def runtime_clock() -> datetime:
            return clock() - timedelta(milliseconds=1)

        settlement = SettlementProcessor(repositories, alerts, clock=runtime_clock)
        market_data = AlpacaMarketDataProvider(broker)
        gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts,
                                   risk_limits_for_profile("balanced"), ExecutionMode.PAPER, clock=runtime_clock,
                                   lease=lease, capabilities=FullBandCapabilities(broker, runtime_clock))
        rt = Runtime(repositories, database, broker, clock, gateway, settlement, market_data, alerts, sent)

        async def record(severity: str, message: str, details: object = None) -> None:
            sent.append((severity, message))

        alerts.send = record  # type: ignore[method-assign]
        await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, T0))
        rt.quote("199.50", "199.60")
        clock.advance(timedelta(seconds=1))
        yield rt
        await broker.aclose()


async def _round_trip(rt: Runtime) -> None:
    """Entry, settlement, and a stop exit an hour later."""
    entry = await rt.buy()
    assert (entry.status, entry.filled_quantity) == ("filled", Decimal("5"))
    rt.advance_to(T0 + timedelta(hours=1))
    rt.quote("185.00", "185.10")  # below the 190 stop
    rt.clock.advance(timedelta(seconds=1))
    summary = await rt.monitor()
    assert (summary.status, summary.exits_triggered) == ("ok", 1)
    assert [(r.status, r.filled_quantity) for r in summary.execution_results] == [("filled", Decimal("5"))]


async def test_buy_settles_then_stop_exit_and_reconciliation_is_clean(runtime: Runtime) -> None:
    rt = runtime
    entry = await rt.buy()
    assert (entry.status, entry.filled_quantity) == ("filled", Decimal("5"))
    [lot] = await rt.rows("position_lots")
    assert Decimal(str(lot["remaining_quantity"])) == Decimal("5")
    [holding] = await rt.rows("holdings")
    assert (Decimal(str(holding["quantity"])), Decimal(str(holding["stop_loss"]))) == (Decimal("5"), Decimal("190"))

    rt.advance_to(T0 + timedelta(hours=1))
    rt.quote("185.00", "185.10")
    rt.clock.advance(timedelta(seconds=1))
    summary = await rt.monitor()
    assert summary.exits_triggered == 1 and summary.execution_results[0].status == "filled"
    assert await rt.broker.get_positions() == []
    closing = [i for i in await rt.intents() if i.side == Side.SELL]  # type: ignore[attr-defined]
    assert [(i.status, i.risk_snapshot.get("exit_reason")) for i in closing] == [  # type: ignore[attr-defined]
        (TradeIntentStatus.FILLED, "stop_loss")]

    rt.advance_to(AFTER_FEES)
    result = await rt.reconcile()
    assert (result.status, result.accounting_drift_detected, result.missed_fills_detected) == ("ok", 0, 0)
    fee_ids = await rt.fee_ids()
    assert len(fee_ids) == 3  # REG, TAF and CAT for the day
    assert await rt.fee_ledger_entries() == Counter({fee_id: 1 for fee_id in fee_ids})
    assert [row["fee_accounting_status"] for row in await rt.rows("accounting_epochs")] == ["reconciled_net"]
    assert await rt.unresolved() == []
    assert rt.critical() == []


async def test_replayed_fee_batch_is_never_double_counted(runtime: Runtime) -> None:
    rt = runtime
    await _round_trip(rt)
    rt.advance_to(AFTER_FEES)
    assert (await rt.reconcile()).status == "ok"
    ledger, attributions = await rt.fee_ledger_entries(), await rt.rows("trade_attributions")
    drift = await rt.drift_records()
    for hour in range(1, 4):  # the same fee activities are read again on every pass
        rt.advance_to(AFTER_FEES + timedelta(hours=hour))
        await rt.settlement.process_pending()
        result = await rt.reconcile()
        assert (result.status, result.accounting_drift_detected) == ("ok", 0)
    assert await rt.fee_ledger_entries() == ledger and set(ledger.values()) == {1}
    assert await rt.rows("trade_attributions") == attributions
    assert await rt.drift_records() == drift  # the replays add no drift
    assert await rt.unresolved() == []
    assert rt.critical() == []


async def test_fee_posted_before_an_existing_cursor_is_captured(runtime: Runtime) -> None:
    """F5: the day's fee batch posts after the close with IDs dated the
    trade date at midnight -- before the activity cursor, which already sits
    on the day's last fill."""
    rt = runtime
    await _round_trip(rt)
    rt.advance_to(CLOSE + timedelta(minutes=5))
    pending = await rt.reconcile()  # the position is closed, its fees have not posted
    assert (pending.status, pending.error) == ("degraded", "ACCOUNTING_EPOCH_INCOMPLETE")
    # The asset-fee cursor; Rev.140's fill cursor shares the table (its payload carries pass_id).
    [cursor_row] = [row for row in await rt.rows("broker_activity_cursors") if "pass_id" not in row]
    cursor = json.dumps(cursor_row)
    last_fill = (await rt.broker.get_activities(activity_type="FILL"))[-1].activity_id
    assert last_fill in cursor and await rt.fee_ledger_entries() == Counter()

    rt.advance_to(AFTER_FEES)
    fee_ids = await rt.fee_ids()
    assert fee_ids and all(fee_id < last_fill for fee_id in fee_ids)  # behind the cursor
    assert (await rt.reconcile()).status == "ok"
    assert await rt.fee_ledger_entries() == Counter({fee_id: 1 for fee_id in fee_ids})
    assert [row["fee_accounting_status"] for row in await rt.rows("accounting_epochs")] == ["reconciled_net"]
    assert await rt.unresolved() == []


async def test_ambiguous_submission_is_recovered_without_a_second_order(runtime: Runtime) -> None:
    rt = runtime
    rt.broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    entry = await rt.buy()
    assert (entry.status, entry.filled_quantity) == ("filled", Decimal("5"))
    assert len(rt.broker._orders) == 1
    rt.advance_to(AFTER_FEES)
    result = await rt.reconcile()
    assert (result.status, result.accounting_drift_detected) == ("ok", 0)
    assert len(rt.broker._orders) == 1
    assert [i.status for i in await rt.intents()] == [TradeIntentStatus.FILLED]  # type: ignore[attr-defined]
    assert await rt.unresolved() == []


async def test_lagged_fill_activity_is_attributed_once_visible(runtime: Runtime) -> None:
    """The order is filled before its FILL activity is visible: the gateway
    will not finalize on unattributed quantity, and leaves the intent for
    reconciliation, which attributes the fill once the activity appears --
    exactly once, however many passes follow."""
    rt = runtime
    rt.broker.set_activity_lag(timedelta(seconds=30))
    rt.gateway.FILL_TIMEOUT_SECONDS = 1  # the gateway's real-time poll budget; the broker clock is manual
    rt.gateway.POLL_INTERVAL_SECONDS = 0.05  # type: ignore[assignment]
    entry = await rt.buy()
    assert (entry.status, entry.filled_quantity) == ("pending", Decimal("0"))
    assert await rt.rows("fills") == []

    rt.clock.advance(timedelta(seconds=30))
    for _ in range(3):
        assert (await rt.reconcile()).status == "ok"
        rt.clock.advance(timedelta(minutes=1))
    assert len(await rt.rows("fills")) == 1
    [intent] = await rt.intents()
    assert (intent.status, intent.filled_quantity) == (TradeIntentStatus.FILLED, Decimal("5"))  # type: ignore[attr-defined]
    assert len(rt.broker._orders) == 1


async def test_out_of_session_exit_is_deferred_and_taken_at_the_open(runtime: Runtime) -> None:
    rt = runtime
    entry = await rt.buy()
    assert entry.status == "filled"
    rt.advance_to(CLOSE + timedelta(hours=1))
    rt.quote("185.00", "185.10")  # below the stop, after the close
    rt.clock.advance(timedelta(seconds=1))
    deferred = await rt.monitor()
    assert (deferred.exits_triggered, deferred.deferred_exits) == (0, 1)
    assert len(rt.broker._orders) == 1  # no exit order while the market is closed

    rt.advance_to(NEXT_OPEN)
    rt.quote("184.00", "184.10")
    rt.clock.advance(timedelta(seconds=1))
    taken = await rt.monitor()
    assert taken.exits_triggered == 1 and [r.status for r in taken.execution_results] == ["filled"]
    assert await rt.broker.get_positions() == []
    exit_order = max(rt.broker._orders.values(), key=lambda o: o.seq)
    assert exit_order.side == Side.SELL and exit_order.filled_at is not None and exit_order.filled_at >= NEXT_OPEN
