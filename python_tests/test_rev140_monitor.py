"""Rev.140 task 8: the monitor never trades negative or quarantined inventory (spec §3.4, test 8)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from rev140_support import AAPL, Rt

from tradepulse.broker.simulated import Fault
from tradepulse.broker.types import AlpacaOrderRequest
from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Holding, Side, asset_identity_key
from tradepulse.models.quarantine import ShortExposureLiability
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.providers import AlpacaMarketDataProvider


async def _monitor(rt: Rt):  # type: ignore[no-untyped-def]
    return await run_position_monitor(rt.repositories, rt.broker, AlpacaMarketDataProvider(rt.broker), rt.gateway, rt.alerts,
                                      risk_limits_for_profile("balanced"), clock=lambda: rt.clock() - timedelta(milliseconds=1))


async def _events(rt: Rt, event_type: str) -> list[object]:
    return [e for e in (hydrate("audit_events", r["payload"]) for r in await paginate_all_rows(rt.repositories.audit_events))
            if e.event_type == event_type]


async def test_negative_position_never_gets_an_order(rt: Rt) -> None:
    """Spec test 8: five passes over a short position; no order of any kind,
    one alert for the day, and never the misleading unmanaged-position alert."""
    rt.broker.inject(Fault.ACCEPT_OVERSELL)
    await rt.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(2), side=Side.SELL, client_order_id="external-short"))
    rt.quote()
    holding = Holding(asset=AAPL, quantity=Decimal(-2), average_price=Decimal(200), updated_at=rt.clock(),
                      stop_loss=Decimal(150), target_price=Decimal(250))  # would "breach" at 199.50 as an inverted short
    await rt.repositories.holdings.create_once(asset_identity_key(AAPL), holding)
    orders_before = len(rt.broker._orders)
    for _ in range(5):
        rt.quote()
        summary = await _monitor(rt)
        assert summary.execution_results == []
    assert len(rt.broker._orders) == orders_before
    assert len(await _events(rt, "unsupported_short_position")) == 1
    assert await _events(rt, "unmanaged_broker_position") == []


async def test_quarantined_asset_skipped_other_asset_stop_fires(rt: Rt) -> None:
    msft = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    rt.broker.set_quote("MSFT", AssetClass.EQUITY, Decimal(400), Decimal("400.10"), at=rt.clock())
    rt.quote()
    await rt.buy("10")  # AAPL, then quarantined below
    rt.broker.set_quote("MSFT", AssetClass.EQUITY, Decimal(400), Decimal("400.10"), at=rt.clock())
    rt.clock.advance(timedelta(seconds=1))
    from tradepulse.execution import ExecutionRequest
    entry = await rt.gateway.execute_intent(ExecutionRequest(asset=msft, side=Side.BUY, requested_quantity=Decimal(2),
                                                             strategy="test", confidence=Decimal(90),
                                                             stop_loss=Decimal(380), target_price=Decimal(450)))
    assert entry.status == "filled", entry
    liability = ShortExposureLiability(liability_id="liability:x", fill_id="x", broker_order_id="ox", trade_intent_id="ix",
                                       asset=AAPL, quantity=Decimal(1), sale_price=Decimal(200), multiplier=Decimal(1),
                                       proceeds=Decimal(200), opened_at=rt.clock())
    await rt.repositories.short_exposure_liabilities.create_once(liability.liability_id, liability, unique_value=liability.fill_id)
    rt.broker.set_quote("AAPL", AssetClass.EQUITY, Decimal(100), Decimal("100.10"), at=rt.clock() + timedelta(seconds=1))
    rt.broker.set_quote("MSFT", AssetClass.EQUITY, Decimal(300), Decimal("300.10"), at=rt.clock() + timedelta(seconds=1))
    rt.clock.advance(timedelta(seconds=2))  # both stops breached
    summary = await _monitor(rt)
    sells = [o for o in rt.broker._orders.values() if o.side == Side.SELL]
    assert [o.symbol for o in sells] == ["MSFT"]
    assert summary.exits_triggered == 1
    assert len(await _events(rt, "quarantined_inventory")) == 1
