"""Phase 1 exit ladder: profit-aware exits in multiples of R (monitor/coordinator.py::plan_ladder_exit)."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Holding, PositionLot
from tradepulse.monitor.coordinator import plan_ladder_exit

LIMITS = risk_limits_for_profile("balanced")
TODAY = date(2026, 10, 20)
OPENED = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
CALL = AssetIdentity("AAPL261120C00200000", AssetClass.OPTION, "alpaca:AAPL261120C00200000",
                     metadata={"contract_multiplier": "100", "expiry": "2026-11-20"})


def _holding(asset=AAPL, quantity="10", average="100", stop="97", **kw) -> Holding:
    # R = 100 - 97 = 3 per unit
    return Holding(asset, Decimal(quantity), Decimal(average), OPENED, stop_loss=Decimal(stop) if stop else None, **kw)


def _lot(asset=AAPL, opened=OPENED, mfe=None, quantity="10", price="100") -> PositionLot:
    return PositionLot("lot-1", "fill-1", asset, "long", Decimal(quantity), Decimal(quantity), Decimal(price), opened,
                       mfe_price=Decimal(mfe) if mfe else None)


def _plan(holding, price, *, lots=None, qty=None, today=TODAY, limits=LIMITS):
    return plan_ladder_exit(holding, lots if lots is not None else [_lot()], Decimal(qty or holding.quantity),
                            Decimal(price), today, limits)


def test_below_one_r_holds():
    assert _plan(_holding(), "102.99") is None


def test_one_r_sells_half_and_marks_scale_out():
    decision = _plan(_holding(), "103")
    assert (decision.quantity, decision.reason, decision.scale_out) == (Decimal("5"), "scale_out", True)


def test_fractional_half_floors_to_order_precision():
    decision = _plan(_holding(quantity="9.877"), "103.5")
    assert decision.quantity == Decimal("4.938")


def test_after_scale_out_only_the_final_target_sells():
    held = _holding(scaled_out=True, current_stop=Decimal("100"))
    assert _plan(held, "105") is None            # +1.67R: the rest rides on the trailing stop
    final = _plan(held, "109")                   # +3R
    assert (final.quantity, final.reason, final.scale_out) == (Decimal("10"), "target_r", False)


def test_final_target_without_a_prior_scale_out_sells_everything():
    decision = _plan(_holding(), "109.5")
    assert (decision.quantity, decision.reason) == (Decimal("10"), "target_r")


def test_a_single_option_contract_exits_whole_at_one_and_a_half_r():
    held = _holding(asset=CALL, quantity="1", average="3.00", stop="1.95")  # R = 1.05 per contract unit
    lots = [_lot(asset=CALL, quantity="1", price="3.00")]
    assert _plan(held, "4.20", lots=lots) is None             # +1.14R: a single contract is not split
    decision = _plan(held, "4.60", lots=lots)                  # +1.52R
    assert (decision.quantity, decision.reason) == (Decimal("1"), "target_r_single_unit")


def test_several_option_contracts_split_in_whole_contracts():
    held = _holding(asset=CALL, quantity="5", average="3.00", stop="1.95")
    decision = _plan(held, "4.05", lots=[_lot(asset=CALL, quantity="5", price="3.00")])  # +1R
    assert (decision.quantity, decision.scale_out) == (Decimal("2"), True)


def test_a_position_too_small_to_split_is_treated_as_one_unit():
    held = _holding(quantity="0.015")  # half would be 0.007 shares, about $0.72 -- below the $1 minimum lot
    assert _plan(held, "103") is None
    assert _plan(held, "104.5").reason == "target_r_single_unit"


def test_a_stalled_trade_is_closed_after_the_stall_window():
    old = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)  # exactly stall_exit_days (7) before TODAY
    lots = [_lot(opened=old, mfe="101.4")]  # best so far +0.47R, never reached +0.5R
    assert _plan(_holding(), "100.5", lots=lots).reason == "stall"
    assert _plan(_holding(), "100.5", lots=lots, today=TODAY - timedelta(days=1)) is None  # one day short
    assert _plan(_holding(), "100.5", lots=[_lot(opened=old, mfe="101.5")]) is None       # it did reach +0.5R once


def test_no_stop_or_no_positive_risk_means_no_ladder():
    assert _plan(_holding(stop=None), "150") is None
    assert _plan(_holding(stop="100"), "150") is None


def test_each_rung_can_be_disabled():
    off = replace(LIMITS, scale_out_r=Decimal("0"), final_target_r=Decimal("0"),
                  single_unit_target_r=Decimal("0"), stall_exit_days=0)
    old = OPENED - timedelta(days=30)
    assert _plan(_holding(), "120", lots=[_lot(opened=old)], limits=off) is None
