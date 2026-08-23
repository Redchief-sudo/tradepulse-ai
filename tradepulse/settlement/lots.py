"""FIFO signed-lot fill planning -- port of base44/shared/signedLots.ts.

Supports both long and short closures: an opposite-direction fill closes
existing lots oldest-first (FIFO), and any unconsumed quantity opens a new
lot in the fill's own direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tradepulse.models import PositionLot, SettlementEvent, Side


class IntegrityViolationError(RuntimeError):
    """Message MUST start with 'INTEGRITY_VIOLATION' -- settlement/stages.py
    classifies failures by that exact prefix to route them to the permanent
    integrity_blocked state instead of a retryable one."""


@dataclass(frozen=True, slots=True)
class LotClosure:
    lot: PositionLot
    quantity: Decimal
    pnl: Decimal


@dataclass(frozen=True, slots=True)
class SignedLotPlan:
    opening_direction: Literal["long", "short"]
    closures: list[LotClosure]
    opening_quantity: Decimal
    realized_pnl: Decimal


def plan_signed_lot_fill(lots: list[PositionLot], event: SettlementEvent) -> SignedLotPlan:
    opening_direction: Literal["long", "short"] = "long" if event.side == Side.BUY else "short"
    closing_direction: Literal["long", "short"] = "short" if opening_direction == "long" else "long"

    # Replay protection: if this event's fill_id already closed against some
    # lots (a resumed/retried settlement run), don't double-close them --
    # count what it already did and treat that as "spent" quantity.
    already_closed = Decimal("0")
    realized_pnl = Decimal("0")
    for lot in lots:
        qty = lot.closures.get(event.fill_id)
        if qty is None:
            continue
        already_closed += qty
        if lot.position_side == "long":
            realized_pnl += (event.price - lot.acquisition_price) * qty
        else:
            realized_pnl += (lot.acquisition_price - event.price) * qty

    already_opened = sum(
        (lot.opened_quantity for lot in lots if lot.originating_fill_id == event.fill_id and lot.position_side == opening_direction),
        Decimal("0"),
    )

    # Decimal arithmetic is exact (no float rounding), so no epsilon slack is
    # needed here unlike the ported TS version.
    if already_closed + already_opened > event.quantity:
        raise IntegrityViolationError(
            f"INTEGRITY_VIOLATION: FILL_EVENT_OVERALLOCATED {already_closed + already_opened} > {event.quantity}"
        )

    remaining = event.quantity - already_closed - already_opened
    closures: list[LotClosure] = []
    opposite_lots = sorted(
        (lot for lot in lots if lot.position_side == closing_direction and lot.status in ("open", "partially_closed")),
        key=lambda lot: lot.opened_at,
    )
    for lot in opposite_lots:
        if remaining <= 0:
            break
        quantity = min(lot.remaining_quantity, remaining)
        if quantity <= 0:
            continue
        pnl = (
            (event.price - lot.acquisition_price) * quantity
            if closing_direction == "long"
            else (lot.acquisition_price - event.price) * quantity
        )
        closures.append(LotClosure(lot=lot, quantity=quantity, pnl=pnl))
        realized_pnl += pnl
        remaining -= quantity

    return SignedLotPlan(
        opening_direction=opening_direction,
        closures=closures,
        opening_quantity=remaining if remaining > 0 else Decimal("0"),
        realized_pnl=realized_pnl,
    )
