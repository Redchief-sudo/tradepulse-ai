from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, PositionLot, SettlementEvent, SettlementStatus, Side
from tradepulse.settlement.lots import IntegrityViolationError, plan_signed_lot_fill


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def asset() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


def fill_event(side: Side, quantity: str, price: str, fill_id: str = "fill-1") -> SettlementEvent:
    return SettlementEvent(
        "se-1", fill_id, "ti-1", asset(), side, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), NOW,
    )


def lot(lot_id: str, position_side: str, opened: str, remaining: str, price: str, opened_at=NOW, closures=None) -> PositionLot:
    return PositionLot(
        lot_id, f"origin-{lot_id}", asset(), position_side, Decimal(opened), Decimal(remaining), Decimal(price),
        opened_at, closures=closures or {},
    )


def test_buy_with_no_opposite_lots_opens_a_new_long_lot() -> None:
    plan = plan_signed_lot_fill([], fill_event(Side.BUY, "10", "150"))
    assert plan.opening_direction == "long"
    assert plan.closures == []
    assert plan.opening_quantity == Decimal("10")
    assert plan.realized_pnl == Decimal("0")


def test_sell_closes_long_lots_fifo_oldest_first() -> None:
    older = lot("lot-1", "long", "5", "5", "100", opened_at=NOW - timedelta(days=2))
    newer = lot("lot-2", "long", "5", "5", "120", opened_at=NOW - timedelta(days=1))
    plan = plan_signed_lot_fill([newer, older], fill_event(Side.SELL, "6", "130"))
    assert [c.lot.lot_id for c in plan.closures] == ["lot-1", "lot-2"]
    assert plan.closures[0].quantity == Decimal("5")  # older lot fully closed first
    assert plan.closures[1].quantity == Decimal("1")  # remainder from the newer lot
    # realized pnl: (130-100)*5 + (130-120)*1 = 150 + 10 = 160
    assert plan.realized_pnl == Decimal("160")
    assert plan.opening_quantity == Decimal("0")


def test_buy_covering_a_short_realizes_pnl_with_inverted_sign() -> None:
    short_lot = lot("lot-1", "short", "10", "10", "100")
    plan = plan_signed_lot_fill([short_lot], fill_event(Side.BUY, "4", "90"))
    assert plan.closures[0].quantity == Decimal("4")
    # covering a short: pnl = (acquisition_price - price) * qty = (100-90)*4 = 40
    assert plan.realized_pnl == Decimal("40")
    assert plan.opening_quantity == Decimal("0")


def test_buy_larger_than_short_position_closes_it_and_opens_a_new_long() -> None:
    short_lot = lot("lot-1", "short", "4", "4", "100")
    plan = plan_signed_lot_fill([short_lot], fill_event(Side.BUY, "10", "90"))
    assert plan.closures[0].quantity == Decimal("4")
    assert plan.opening_direction == "long"
    assert plan.opening_quantity == Decimal("6")


def test_replayed_event_does_not_double_close_an_already_closed_lot() -> None:
    # This lot already recorded a closure allocation for fill-1 (a resumed
    # settlement run replaying the same event) -- re-running must not close
    # it a second time.
    already_closed_lot = lot("lot-1", "long", "10", "5", "100", closures={"fill-1": Decimal("5")})
    plan = plan_signed_lot_fill([already_closed_lot], fill_event(Side.SELL, "5", "130", fill_id="fill-1"))
    assert plan.closures == []  # nothing further to close -- already accounted for
    assert plan.realized_pnl == Decimal("150")  # (130-100)*5, replayed from the existing allocation


def test_overallocated_fill_raises_integrity_violation() -> None:
    already_closed_lot = lot("lot-1", "long", "10", "5", "100", closures={"fill-1": Decimal("5")})
    other_lot = lot("lot-2", "long", "10", "10", "100", closures={"fill-1": Decimal("10")})
    with pytest.raises(IntegrityViolationError, match="INTEGRITY_VIOLATION"):
        plan_signed_lot_fill([already_closed_lot, other_lot], fill_event(Side.SELL, "5", "130", fill_id="fill-1"))
