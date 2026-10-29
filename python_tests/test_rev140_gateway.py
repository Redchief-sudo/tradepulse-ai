"""Rev.140 task 6: sell authorization by the conservative bound under a mandatory
reservation (spec §3.1, §3.1b, tests 2, 4, 4b, 4f-4j, 4p-4r)."""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rev140_support import AAPL, KEY, T0, Rt

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import Fault, ManualClock, SimSession, SimulatedBroker
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest, ExecutionResult
from tradepulse.execution.idempotency import release_symbol_reservation, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    ReconciliationOutcome,
    ReconciliationRecord,
    SessionState,
    Side,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation import run_reconciliation
from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
from tradepulse.reconciliation.unresolved import record_pass
from tradepulse.risk import save_session
from tradepulse.runtime_lease import account_lease
from tradepulse.settlement import SettlementProcessor


async def _held(rt: Rt, quantity: str = "10") -> None:
    entry = await rt.buy(quantity)
    assert (entry.status, entry.filled_quantity) == ("filled", Decimal(quantity)), entry
    rt.quote()


async def test_sell_without_token_refused_before_reads(rt: Rt) -> None:
    """Spec 4i."""
    await _held(rt)
    rt.calls.clear()
    result = await rt.sell("5", None)
    assert (result.status, result.reasons, result.trade_intent_id) == ("rejected", ["EXECUTION_RESERVATION_REQUIRED"], None)
    assert rt.calls == []


async def test_a_reservation_held_by_another_owner_refuses(rt: Rt) -> None:
    await _held(rt)
    assert await reserve_symbol_for_execution(rt.database, AAPL, "holder")
    result = await rt.sell("5", "intruder", reserve=False)
    assert result.reasons == ["EXECUTION_RESERVATION_LOST"]
    assert not [i for i in await rt.intents() if i.side == Side.SELL]


async def test_two_protective_exits_at_once_one_reservation(rt: Rt) -> None:
    """Spec 4f: whichever of two simultaneous exits wins the reservation sells;
    the other never reaches the broker."""
    await _held(rt)

    async def attempt(token: str) -> ExecutionResult | None:
        if not await reserve_symbol_for_execution(rt.database, AAPL, token):
            return None
        try:
            return await rt.gateway.execute_intent(ExecutionRequest(
                asset=AAPL, side=Side.SELL, requested_quantity=Decimal(10), strategy="monitor_exit", decision_id=token,
                symbol_lock_owner_token=token))
        finally:
            await release_symbol_reservation(rt.database, AAPL, token)

    results = await asyncio.gather(attempt("a"), attempt("b"))
    assert sum(r is not None and r.status == "filled" for r in results) == 1
    assert sum(o.qty for o in await rt.exits_at_broker()) <= Decimal(10)
    assert await rt.position() >= 0


async def test_reduced_exit_then_competitor_gets_zero(rt: Rt) -> None:
    """Spec 4g: a protective exit above sellable is reduced, with an alert;
    the next one finds nothing left."""
    await _held(rt, "5")
    first = await rt.sell("10", "first", decision="d1")
    assert (first.status, first.filled_quantity) == ("filled", Decimal(5))
    assert any("SELLABLE_BELOW_REQUESTED" in message for _, message in rt.sent)
    second = await rt.sell("5", "second", decision="d2")
    assert second.status == "rejected" and second.reasons[0].startswith("INSUFFICIENT_SELLABLE_QUANTITY")
    assert await rt.position() == 0


async def test_unsettled_terminal_sell_is_counted_with_positions_lagging(rt: Rt) -> None:
    """Spec 4: a TradePulse sell that filled at the broker, has left the open
    orders and is not yet settled stays in Ω, while positions still show the
    shares."""
    await _held(rt)
    first = await rt.sell("10", "first", decision="d1")
    assert first.status == "filled"
    intent = next(i for i in await rt.intents() if i.side == Side.SELL)
    rows = [r for r in await rt.repositories.settlements.list_all() if r["payload"]["trade_intent_id"] == intent.trade_intent_id]
    for row in rows:  # as if settlement had not run yet
        await rt.database.run(lambda c, rid=row["record_id"]: c.execute("UPDATE settlements SET payload=json_set(payload,'$.status','pending'), status='pending' WHERE record_id=?", (rid,)), write=True)
    rt.broker.lag_positions(1)
    second = await rt.sell("10", "second", decision="d2")
    assert second.status == "rejected" and second.reasons[0].startswith("INSUFFICIENT_SELLABLE_QUANTITY")


async def test_unstable_three_times_refuses_unchanged(rt: Rt) -> None:
    """Spec 4b: an order of the asset keeps changing between the two
    open-order reads; after three attempts the sell waits, writing nothing."""
    await _held(rt)
    rt.quote("150", "150.10")  # an external limit order rests above the market
    from tradepulse.broker.types import AlpacaOrderRequest
    resting = await rt.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(2), side=Side.SELL,
                                                             client_order_id="external", order_type="limit",
                                                             limit_price=Decimal(300)))
    flips = iter(range(3))

    def move() -> None:
        next(flips)
        order = rt.broker._orders[resting.broker_order_id]
        order.status = "partially_filled" if order.status != "partially_filled" else "new"

    for _ in range(3):
        rt.broker.step_before("get_open_orders", lambda: None)  # before O1
        rt.broker.step_before("get_open_orders", move)  # before O2
    intents_before = len(await rt.intents())
    result = await rt.sell("5", "seller")
    assert result.reasons == ["SELLABLE_SNAPSHOT_UNSTABLE"]
    assert len(await rt.intents()) == intents_before


async def test_external_fill_between_reads_caught_by_activities(rt: Rt) -> None:
    """Spec 4b: an execution TradePulse did not make, listed after C."""
    await _held(rt)
    await run_reconciliation(rt.repositories, rt.broker, rt.settlement, rt.alerts, clock=lambda: rt.clock())
    from tradepulse.broker.types import AlpacaOrderRequest
    await rt.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(1), side=Side.SELL, client_order_id="external"))
    rt.quote()
    result = await rt.sell("5", "seller")
    assert result.reasons == ["UNRECONCILED_BROKER_ACTIVITY"]


async def test_buy_against_negative_position_refused(rt: Rt) -> None:
    """The cover path is removed."""
    from tradepulse.broker.types import AlpacaOrderRequest
    rt.broker.inject(Fault.ACCEPT_OVERSELL)
    await rt.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(3), side=Side.SELL, client_order_id="short"))
    rt.quote()
    result = await rt.buy("3")
    assert (result.status, result.reasons) == ("rejected", ["UNSUPPORTED_SHORT_POSITION"])


async def test_history_unproven_refuses_sell_before_any_broker_read(rt: Rt) -> None:
    await _held(rt)
    record = ReconciliationRecord(record_id="activity_history:p1:test", reconciliation_type="activity_history", subject_id="p1",
                                  outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={}, actual={"failures": [KEY], "full": False},
                                  occurred_at=rt.clock())
    await rt.repositories.reconciliation_records.create_once(record.record_id, record)
    rt.calls.clear()
    assert (await rt.sell("5", "seller")).reasons == ["ACTIVITY_HISTORY_UNPROVEN"]
    assert rt.calls == []


async def test_unresolved_execution_refuses_sell(rt: Rt) -> None:
    from tradepulse.broker import AlpacaActivity
    await _held(rt)
    raw = {"id": "20261019110000000::00000000-0000-4000-8000-000000000001", "activity_type": "FILL", "symbol": "AAPL",
           "side": "sell", "qty": "1", "price": "199", "transaction_time": rt.clock().isoformat()}
    activity = AlpacaActivity(raw["id"], "FILL", "AAPL", Side.SELL, Decimal(1), Decimal(199), rt.clock(), raw)
    await record_pass(rt.repositories, [activity], page_proven=True, pass_id="p", now=rt.clock())
    assert (await rt.sell("5", "seller")).reasons == ["UNRESOLVED_BROKER_EXECUTION"]


async def test_lease_expires_during_place_order(rt: Rt) -> None:
    """Spec 4p: the first exit is inside place_order when its reservation
    lapses. A second holder is refused while the first is unresolved; the
    first records its own outcome; exactly one exit reaches the broker."""
    await _held(rt)
    release = rt.broker.hold_call("place_order")
    assert await reserve_symbol_for_execution(rt.database, AAPL, "first")
    first = asyncio.create_task(rt.gateway.execute_intent(ExecutionRequest(
        asset=AAPL, side=Side.SELL, requested_quantity=Decimal(10), strategy="monitor_exit", decision_id="d1",
        symbol_lock_owner_token="first")))
    for _ in range(500):  # the first exit's database calls run in threads
        await asyncio.sleep(0.01)
        if await rt.exits_at_broker():
            break
    assert len(await rt.exits_at_broker()) == 1
    await rt.database.run(lambda c: c.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00+00:00'"), write=True)
    second = await rt.sell("10", "second", decision="d2")
    assert second.reasons == ["PRIOR_SUBMISSION_UNRESOLVED"]
    release()
    assert (await first).status == "filled"
    assert len(await rt.exits_at_broker()) == 1 and await rt.position() == 0


async def test_ambiguous_submission_counts_in_full_until_recovered(rt: Rt) -> None:
    """Spec 4q: an accepted-but-ambiguous exit is submission_unknown; it keeps
    its full obligation and every later exit is refused until recovered."""
    await _held(rt)
    rt.broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    rt.broker.inject(Fault.LOOKUP_TIMEOUT, times=5)
    first = await rt.sell("10", "first", decision="d1")
    intent = next(i for i in await rt.intents() if i.side == Side.SELL)
    assert intent.status == TradeIntentStatus.SUBMISSION_UNKNOWN, first
    second = await rt.sell("10", "second", decision="d2")
    assert second.reasons == ["PRIOR_SUBMISSION_UNRESOLVED"]
    assert len(await rt.exits_at_broker()) == 1


async def test_oversell_accepting_broker_never_reached(rt: Rt) -> None:
    """Spec test 2: with a broker that accepts every oversell, 200 seeded
    exits of random size never take the position below zero."""
    rng = random.Random(140)
    rt.broker.inject(Fault.ACCEPT_OVERSELL, times=1000)
    await _held(rt)
    for n in range(200):
        if rng.random() < 0.3:
            await rt.buy(str(rng.randint(1, 5)))
        else:
            await rt.sell(str(rng.randint(1, 15)), f"t{n}", decision=f"d{n}")
        rt.quote()
        assert await rt.position() >= 0
    assert not any(o.overfilled for o in rt.broker._orders.values())


async def test_seeded_concurrency_never_oversells(rt: Rt) -> None:
    """Spec 4j: 50 seeded trials. Two exits race for one reservation, with
    random position lag, against a broker that would accept any oversell."""
    rng = random.Random(4)
    rt.broker.inject(Fault.ACCEPT_OVERSELL, times=1000)
    for trial in range(50):
        await rt.buy(str(rng.randint(1, 8)))
        rt.quote()
        rt.broker.lag_positions(rng.choice([0, 0, 1]))

        async def attempt(token: str, trial: int = trial) -> None:
            if await reserve_symbol_for_execution(rt.database, AAPL, token):
                try:
                    await rt.gateway.execute_intent(ExecutionRequest(
                        asset=AAPL, side=Side.SELL, requested_quantity=Decimal(rng.randint(1, 10)), strategy="monitor_exit",
                        decision_id=f"{trial}-{token}", symbol_lock_owner_token=token))
                finally:
                    await release_symbol_reservation(rt.database, AAPL, token)

        await asyncio.gather(attempt(f"a{trial}"), attempt(f"b{trial}"))
        rt.broker.lag_positions(0)
        rt.quote()
        assert await rt.position() >= 0


async def test_crypto_cfee_lag_reduces_not_refuses(tmp_path: Path) -> None:
    """Review Focus 2: the broker deducts a coin fee before local lots record
    it. Sellable is the broker quantity; the exit is reduced, never refused,
    and never sells above it."""
    import decimal

    from tradepulse.broker.simulated import FeePosting, FeeRates, FeeScenario

    rates = FeeRates(reg_per_dollar=Decimal(0), taf_per_share=Decimal(0), taf_max_per_trade=Decimal(0),
                     opt_taf_per_contract=Decimal(0), cat_per_share=Decimal(0), orf_per_contract=Decimal(0),
                     occ_per_contract=Decimal(0), crypto_rate=Decimal("0.0025"), rounding=decimal.ROUND_UP)
    posting = FeePosting(equity_batch_ny=datetime.min.time().replace(hour=20), option_batch_ny=datetime.min.time().replace(hour=16, minute=31),
                         occ_delay=timedelta(seconds=12), cfee_asset_delay=timedelta(seconds=3), cfee_usd_delay=timedelta(hours=4))
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/crypto.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=SimSession.from_exchange_calendar("XNYS", date(2026, 9, 1), date(2026, 12, 31)),
                             seed=7, opening_journal=True, fee_scenario=FeeScenario.observed_alpaca_2026(rates, posting))
    sent: list[str] = []
    alerts = TelegramAlerter(None, None)

    async def record(severity: str, message: str, details: object = None) -> None:
        sent.append(message)

    alerts.send = record  # type: ignore[method-assign]
    btc = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="crypto", directory=tmp_path) as lease:
        await bootstrap_fill_cursor(repositories, broker, lease, reason="test", now=clock())
        settlement = SettlementProcessor(repositories, alerts, clock=lambda: clock() - timedelta(milliseconds=1))
        gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker), settlement, alerts,
                                   risk_limits_for_profile("balanced"), ExecutionMode.PAPER,
                                   clock=lambda: clock() - timedelta(milliseconds=1), lease=lease)
        await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, T0))
        clock.advance(timedelta(seconds=1))
        broker.set_quote("BTC/USD", AssetClass.CRYPTO, Decimal(60000), Decimal(60010), at=clock())
        clock.advance(timedelta(seconds=1))
        entry = await gateway.execute_intent(ExecutionRequest(asset=btc, side=Side.BUY, requested_quantity=Decimal("0.1"),
                                                              strategy="test", confidence=Decimal(90),
                                                              stop_loss=Decimal(55000), target_price=Decimal(70000)))
        assert entry.status == "filled", entry
        clock.advance(timedelta(seconds=5))  # the coin fee has posted at the broker; local lots do not know yet
        broker.set_quote("BTC/USD", AssetClass.CRYPTO, Decimal(60000), Decimal(60010), at=clock())
        clock.advance(timedelta(seconds=1))
        held = next(p.qty for p in await broker.get_positions() if p.symbol.startswith("BTC"))
        assert held < Decimal("0.1")
        assert await reserve_symbol_for_execution(database, btc, "exit")
        result = await gateway.execute_intent(ExecutionRequest(asset=btc, side=Side.SELL, requested_quantity=Decimal("0.1"),
                                                               strategy="monitor_exit", decision_id="exit",
                                                               exit_reason="stop_loss", symbol_lock_owner_token="exit"))
        await broker.aclose()
    assert result.status == "filled", result
    assert any("SELLABLE_BELOW_REQUESTED" in message for message in sent)
    sold = sum(o.qty for o in broker._orders.values() if o.side == Side.SELL)
    assert sold == held
