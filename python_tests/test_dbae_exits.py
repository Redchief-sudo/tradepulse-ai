"""DBAE Task 8: scale-out feasibility, residual alert and protective-exit recovery (spec §8.4, §8.5, §9)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from rev140_support import AAPL, Rt
from test_dbae_gateway import NOW, _held

from tradepulse.broker.capabilities import AssetTradingCapabilities
from tradepulse.broker.simulated import Fault
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway
from tradepulse.models import AssetClass, AssetIdentity, Holding, TradeIntentStatus, asset_identity_key
from tradepulse.monitor import run_position_monitor
from tradepulse.monitor.coordinator import _scale_out_quantity
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation import run_reconciliation
from tradepulse.settlement import SettlementProcessor

LIMITS = risk_limits_for_profile("balanced")
BTC = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")


def _caps(min_order_size: Decimal | None = None, increment: Decimal | None = None) -> AssetTradingCapabilities:
    return AssetTradingCapabilities(True, True, True, min_order_size, increment, None, NOW)


def _holding(asset: AssetIdentity, quantity: str) -> Holding:
    return Holding(asset, Decimal(quantity), Decimal(1), NOW, stop_loss=Decimal("0.9"))


# --- scale-out feasibility (spec §8.4) ----------------------------------------------------------------


def test_scale_out_skipped_when_part_too_small() -> None:
    """Both parts clear the $1 min lot but not the asset's own minimum (2 x $3 = $6)."""
    holding = _holding(AAPL, "3.9")
    price = Decimal(3)
    assert _scale_out_quantity(holding, Decimal("3.9"), price, LIMITS, None) == Decimal("1.95")  # $1 rule only
    assert _scale_out_quantity(holding, Decimal("3.9"), price, LIMITS, _caps(Decimal(2))) is None
    assert _scale_out_quantity(holding, Decimal(4), price, LIMITS, _caps(Decimal(2))) == Decimal(2)


def test_crypto_scale_out_off_increment_skipped() -> None:
    holding = _holding(BTC, "0.0105")
    price = Decimal(1000)
    caps = _caps(Decimal("0.001"), Decimal("0.001"))
    assert _scale_out_quantity(holding, Decimal("0.0105"), price, LIMITS, caps) is None  # part 0.00525
    assert _scale_out_quantity(_holding(BTC, "0.01"), Decimal("0.01"), price, LIMITS, caps) == Decimal("0.005")
    assert _scale_out_quantity(_holding(BTC, "0.01"), Decimal("0.01"), price, LIMITS, None) is None  # unknown: skipped


# --- helpers for the monitor rig -----------------------------------------------------------------------


async def _monitor(rt: Rt, gateway: ExecutionGateway | None = None) -> Any:
    return await run_position_monitor(rt.repositories, rt.broker, AlpacaMarketDataProvider(rt.broker), gateway or rt.gateway,
                                      rt.alerts, LIMITS, clock=lambda: rt.clock() - timedelta(milliseconds=1))


async def _events(rt: Rt, event_type: str) -> list[Any]:
    return [e for e in (hydrate("audit_events", r["payload"]) for r in await paginate_all_rows(rt.repositories.audit_events))
            if e.event_type == event_type]


async def _breach(rt: Rt, quantity: str = "10", bid: str = "150", ask: str = "150.10") -> None:
    """Hold AAPL with a 190 stop, then move the market through it."""
    assert (await rt.buy(quantity)).status == "filled"
    rt.quote(bid, ask)


# --- residual below the entry minimum (spec §8.5) -----------------------------------------------------


async def test_residual_below_1_alerts_once_and_full_exit_submitted(rt: Rt) -> None:
    assert (await rt.buy("10")).status == "filled"
    rt.quote("0.05", "0.06")  # 10 x $0.05 = $0.50 held, far through the 190 stop
    rt.broker.inject(Fault.SUBMIT_REJECTED)  # the first cycle's exit fails, so the position is seen twice

    first = await _monitor(rt)
    assert first.execution_results[0].status == "rejected"
    rt.quote("0.05", "0.06")
    second = await _monitor(rt)

    assert second.exits_triggered == 1
    [sell] = await rt.exits_at_broker()
    assert sell.qty == Decimal(10)  # the full quantity: the alert never changes the exit
    residual = await _events(rt, "DBAE_RESIDUAL_BELOW_ENTRY_MINIMUM")
    assert [(e.severity, e.entity_id) for e in residual] == [("warning", asset_identity_key(AAPL))]  # once, over two cycles


# --- definitive rejection (spec §9) --------------------------------------------------------------------


async def test_definitive_rejection_keeps_obligation_and_alerts(rt: Rt) -> None:
    await _breach(rt)
    before = await rt.repositories.holdings.get(asset_identity_key(AAPL))
    rt.broker.inject(Fault.SUBMIT_REJECTED)

    first = await _monitor(rt)

    [result] = first.execution_results
    assert result.status == "rejected" and result.reasons[0].startswith("BROKER_SUBMIT_ERROR")
    assert await rt.repositories.holdings.get(asset_identity_key(AAPL)) == before  # holding and stop unchanged
    assert await rt.position() == Decimal(10)
    [event] = await _events(rt, "PROTECTIVE_EXIT_REJECTED")
    assert (event.severity, event.entity_id) == ("critical", result.trade_intent_id)
    assert result.reasons[0].removeprefix("BROKER_SUBMIT_ERROR: ") in event.message

    rt.quote("150", "150.10")
    second = await _monitor(rt)

    [retry] = second.execution_results
    assert retry.status == "filled" and retry.trade_intent_id != result.trade_intent_id
    assert await rt.position() == Decimal(0)


# --- ambiguity and restart (spec §9, existing recovery behaviour) -------------------------------------


async def test_timeout_then_found_no_second_order(rt: Rt) -> None:
    await _breach(rt)
    rt.broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    rt.broker.inject(Fault.LOOKUP_TIMEOUT)

    first = await _monitor(rt)
    assert first.execution_results[0].status == "pending"
    assert len(await rt.exits_at_broker()) == 1

    rt.quote("150", "150.10")
    await _monitor(rt)  # the unresolved intent blocks the asset; the lookup finds the order
    assert len(await rt.exits_at_broker()) == 1
    assert await rt.position() == Decimal(0)


async def test_timeout_never_found_no_second_sell_across_restart(rt: Rt) -> None:
    await _breach(rt)
    rt.broker.inject(Fault.SUBMIT_AMBIGUOUS_NOT_ACCEPTED)

    first = await _monitor(rt)
    assert first.execution_results[0].status == "pending"
    assert [i.status for i in await rt.intents() if i.side.value == "sell"] == [TradeIntentStatus.SUBMISSION_UNKNOWN]

    # A restart: a new gateway over the same database and broker.
    def clock() -> datetime:
        return rt.clock() - timedelta(milliseconds=1)

    settlement = SettlementProcessor(rt.repositories, rt.alerts, clock=clock)
    restarted = ExecutionGateway(rt.repositories, rt.broker, AlpacaMarketDataProvider(rt.broker), settlement, rt.alerts,
                                 LIMITS, rt.gateway._execution_mode, clock=clock, lease=rt.gateway._lease,
                                 capabilities=rt.gateway.capabilities)
    for _ in range(3):
        rt.quote("150", "150.10")
        await _monitor(rt, restarted)

    assert await rt.exits_at_broker() == []
    assert await rt.position() == Decimal(10)
    assert [i.status for i in await rt.intents() if i.side.value == "sell"] == [TradeIntentStatus.SUBMISSION_UNKNOWN]


async def test_reconciliation_proves_terminal_then_new_exit_allowed(rt: Rt) -> None:
    assert (await rt.buy("10")).status == "filled"
    rt.quote("150", "150.10")
    rt.broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    rt.broker.inject(Fault.LOOKUP_TIMEOUT)
    partial = await rt.sell("4", "tok-1", decision="exit-a")
    assert partial.status == "pending"
    blocked = await rt.sell("6", "tok-2", decision="exit-b")
    assert blocked.status != "filled"  # unresolved: no second sell
    assert len(await rt.exits_at_broker()) == 1

    rt.clock.advance(timedelta(minutes=5))  # past the stranded-intent grace period
    rt.quote("150", "150.10")
    await run_reconciliation(rt.repositories, rt.broker, rt.settlement, rt.alerts,
                             clock=lambda: rt.clock() - timedelta(milliseconds=1))

    first_sell = next(i for i in await rt.intents() if i.side.value == "sell")
    assert first_sell.status == TradeIntentStatus.FILLED
    assert await rt.position() == Decimal(6)
    rt.quote("150", "150.10")
    again = await rt.sell("6", "tok-3", decision="exit-c")
    assert again.status == "filled", again.reasons
    assert await rt.position() == Decimal(0)


# --- stops below $20 (spec §9) -------------------------------------------------------------------------


async def test_stop_exit_below_20_has_no_dbae_reason(tmp_path: Path) -> None:
    async with _held(tmp_path, Decimal(15)) as held:
        result = await held.stop_exit(Decimal("0.05"))
        assert result.status == "filled", result.reasons
        assert not any("DBAE" in reason for reason in result.reasons)
        [intent] = await held.rig.intents()
        assert intent.rejection_reason is None and "dbae" not in intent.risk_snapshot
