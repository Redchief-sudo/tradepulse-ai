"""Rev.140 task 9: quarantined inventory in equity, exposure and reconciliation (spec §3.3, §3.5, tests 6, 9, 15)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from rev140_support import AAPL, KEY, Rt

from tradepulse.broker import AlpacaActivity
from tradepulse.broker.simulated import Fault
from tradepulse.broker.types import AlpacaOrderRequest
from tradepulse.models import ExecutionMode, Fill, SessionState, Side
from tradepulse.models.quarantine import ShortExposureLiability
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.quarantine import QuarantinedPosition, quarantined_positions
from tradepulse.reconciliation import run_reconciliation
from tradepulse.risk import load_session
from tradepulse.valuation import marked_snapshot, observe_broker_valuation


def _liability(last: str | None) -> QuarantinedPosition:
    return QuarantinedPosition("liability:f", "short_liability", KEY, -1, Decimal(2), Decimal(1), Decimal(100),
                               Decimal(last) if last else None)


@pytest.mark.parametrize(("current", "exposure", "equity"), [
    ("105", "210", "-210"), ("95", "200", "-200"), ("120", "240", "-240"), ("80", "200", "-200"),
])
def test_liability_marks_up_down_and_gap(current: str, exposure: str, equity: str) -> None:
    """Spec test 9: +5%, -5%, +20%, -20% from a confirmed 100. A liability is
    valued at the higher of the two marks for both exposure and equity."""
    record = _liability("100")
    assert record.exposure(Decimal(current)) == Decimal(exposure)
    assert record.equity_value(Decimal(current)) == Decimal(equity)
    assert record.unrealized_pnl(Decimal(current)) == Decimal(2) * (Decimal(100) - Decimal(current))


def test_unmanaged_long_is_valued_at_the_lower_mark_and_exposed_at_the_higher() -> None:
    record = QuarantinedPosition("unmanaged:c", "unmanaged_long", KEY, 1, Decimal(3), Decimal(1), Decimal(90), Decimal(100))
    assert (record.exposure(Decimal(80)), record.equity_value(Decimal(80))) == (Decimal(300), Decimal(240))
    assert (record.exposure(Decimal(120)), record.equity_value(Decimal(120))) == (Decimal(360), Decimal(300))


def test_without_a_confirmed_mark_the_current_one_is_used() -> None:
    record = _liability(None)
    assert (record.exposure(Decimal(110)), record.equity_value(Decimal(110))) == (Decimal(220), Decimal(-220))


async def _oversold(rt: Rt) -> None:
    await rt.buy("10")
    rt.quote()
    rt.broker.overfill_next(Decimal(2))
    await rt.sell("10", "exit", decision="oversell")
    rt.quote()
    await rt.settlement.process_pending(force_retry=True)


async def _snapshot(rt: Rt):  # type: ignore[no-untyped-def]
    account, positions = await observe_broker_valuation(rt.broker)
    return await marked_snapshot(rt.repositories, account, positions, now=rt.clock())


async def test_snapshot_blocks_present_and_empty_normally(rt: Rt) -> None:
    await rt.buy("5")
    rt.quote()
    snapshot = await _snapshot(rt)
    assert (snapshot.reconciliation_results["short_exposure_liabilities"],
            snapshot.reconciliation_results["unmanaged_long_inventory"]) == ([], [])
    assert snapshot.reconciliation_results["position_quantities"][KEY] == "matched"


async def test_snapshot_after_an_oversell_explains_the_short_to_the_cent(rt: Rt) -> None:
    await _oversold(rt)
    snapshot = await _snapshot(rt)
    (block,) = snapshot.reconciliation_results["short_exposure_liabilities"]
    assert (block["asset_key"], block["open_quantity"]) == (KEY, "2")
    assert Decimal(block["equity_value"]) == -Decimal(2) * Decimal(block["mark"])
    assert snapshot.reconciliation_results["position_quantities"][KEY] == "matched"  # I2: -2 = 0 lots - 2 liability
    assert "unexplained_short_market_value" not in snapshot.valuation_errors
    assert snapshot.equity_reconciliation_difference == 0


async def test_out_of_band_sell_latches_unexplained_short(rt: Rt) -> None:
    """Spec test 6: a short nobody recorded. Reconciliation latches
    UNEXPLAINED_SHORT_POSITION, and no liability is fabricated from a
    position read."""
    rt.broker.inject(Fault.ACCEPT_OVERSELL)
    await rt.broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal(2), side=Side.SELL, client_order_id="out-of-band"))
    rt.quote()
    await run_reconciliation(rt.repositories, rt.broker, rt.settlement, rt.alerts, clock=lambda: rt.clock())
    session = await load_session(rt.repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert "UNEXPLAINED_SHORT_POSITION" in (session.financial_integrity_reason or "")
    assert await quarantined_positions(rt.repositories) == []


async def test_exposure_includes_quarantined_inventory(rt: Rt) -> None:
    from tradepulse.risk import build_portfolio_snapshot

    liability = ShortExposureLiability(liability_id="liability:x", fill_id="x", broker_order_id="ox", trade_intent_id="ix",
                                       asset=AAPL, quantity=Decimal(2), sale_price=Decimal(200), multiplier=Decimal(1),
                                       proceeds=Decimal(400), opened_at=rt.clock())
    await rt.repositories.short_exposure_liabilities.create_once(liability.liability_id, liability, unique_value="x")
    snapshot = await build_portfolio_snapshot(rt.repositories, cash_balance=Decimal(100000), account_equity=Decimal(100000),
                                              mark_prices={KEY: Decimal(210)}, now=rt.clock())
    assert snapshot.holdings_value == Decimal(420)
    assert snapshot.sector_exposure["Quarantined"] == Decimal(420)


async def test_cover_not_heuristically_matched_to_unrelated_buy(rt: Rt) -> None:
    """Spec test 15: on an asset with quarantined inventory, a broker buy is
    never paired with an unrelated local buy by the old time-window
    heuristic -- only an exact activity ID counts."""
    from unittest.mock import AsyncMock

    from tradepulse.reconciliation.coordinator import _reconcile_fills

    liability = ShortExposureLiability(liability_id="liability:x", fill_id="x", broker_order_id="ox", trade_intent_id="ix",
                                       asset=AAPL, quantity=Decimal(2), sale_price=Decimal(200), multiplier=Decimal(1),
                                       proceeds=Decimal(400), opened_at=rt.clock())
    await rt.repositories.short_exposure_liabilities.create_once(liability.liability_id, liability, unique_value="x")
    at = rt.clock()
    local = Fill(fill_id="local-buy", trade_intent_id="unrelated", order_id="o-local", asset=AAPL, side=Side.BUY,
                 execution_mode=ExecutionMode.PAPER, quantity=Decimal(2), price=Decimal(199), fees=Decimal(0),
                 slippage=Decimal(0), filled_at=at, broker_fill_id="another-id")
    await rt.repositories.fills.create_once(local.fill_id, local, unique_value=local.broker_fill_id)
    raw = {"id": "cover-activity", "activity_type": "FILL", "symbol": "AAPL", "side": "buy", "qty": "2", "price": "199",
           "transaction_time": (at + timedelta(seconds=1)).isoformat(), "order_id": "external-cover"}
    broker = AsyncMock()
    broker.get_activities.return_value = [AlpacaActivity("cover-activity", "FILL", "AAPL", Side.BUY, Decimal(2), Decimal(199),
                                                         at + timedelta(seconds=1), raw)]
    checked, missed, _ = await _reconcile_fills(rt.repositories, broker, AsyncMock(), rt.alerts, at, timedelta(days=1))
    assert (checked, missed) == (1, 1)
    records = [hydrate("reconciliation_records", r["payload"]) for r in await paginate_all_rows(rt.repositories.reconciliation_records)]
    assert not [r for r in records if r.subject_id == "cover-activity" and r.outcome.value == "matched"]
