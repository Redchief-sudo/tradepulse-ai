from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping

from .base import decimal_value, immutable_metadata, require_aware, require_text
from .market import AssetIdentity

PositionSide = Literal["long", "short"]
LotStatus = Literal["open", "partially_closed", "closed"]


@dataclass(frozen=True, slots=True)
class Holding:
    asset: AssetIdentity
    quantity: Decimal
    average_price: Decimal
    updated_at: datetime
    sector: str | None = None
    # The entry that opened this position's own chosen protective levels --
    # a TradePulse strategy decision, not a broker account fact, so unlike
    # quantity/average_price this is never sourced from Alpaca. See
    # settlement/engine.py::_project_holding for how these are derived.
    stop_loss: Decimal | None = None
    target_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", decimal_value(self.quantity, "quantity"))
        object.__setattr__(self, "average_price", decimal_value(self.average_price, "average_price", positive=True))
        object.__setattr__(self, "updated_at", require_aware(self.updated_at, "updated_at"))
        if self.quantity == 0:
            raise ValueError("zero quantity is not an open holding")
        if self.stop_loss is not None:
            object.__setattr__(self, "stop_loss", decimal_value(self.stop_loss, "stop_loss", positive=True))
        if self.target_price is not None:
            object.__setattr__(self, "target_price", decimal_value(self.target_price, "target_price", positive=True))


@dataclass(frozen=True, slots=True)
class PositionLot:
    lot_id: str
    originating_fill_id: str
    asset: AssetIdentity
    position_side: PositionSide
    opened_quantity: Decimal
    remaining_quantity: Decimal
    acquisition_price: Decimal
    opened_at: datetime
    # event/fill id -> quantity of THIS lot already closed by that event.
    # Tracked so a replayed settlement event (retry after a crash) can tell
    # it already applied its allocation to this lot and not double-close it
    # -- the typed equivalent of Base44's closure_fill_ids JSON-string hack,
    # which existed only because its entity fields couldn't hold nested data.
    closures: Mapping[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    # Outcome Attribution -- running price extremes observed while this lot
    # was open, folded in from two independent sources: settlement's own
    # opening/closing fill prices (settlement/engine.py::_project_lot) and
    # the position monitor's periodic broker-price observations
    # (monitor/coordinator.py::run_position_monitor). "Favorable"/"adverse"
    # are direction-aware per position_side: for a long lot mfe_price is the
    # highest price seen (mfe_price >= mae_price); for a short lot it's the
    # lowest (mfe_price <= mae_price) -- no cross-field invariant is
    # enforced here since the direction flips with position_side. Both
    # additive/optional so every pre-existing PositionLot construction
    # (tests, legacy rows) stays valid unchanged.
    mfe_price: Decimal | None = None
    mae_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.position_side not in ("long", "short"):
            raise ValueError("position_side must be 'long' or 'short'")
        object.__setattr__(self, "lot_id", require_text(self.lot_id, "lot_id"))
        object.__setattr__(self, "originating_fill_id", require_text(self.originating_fill_id, "originating_fill_id"))
        object.__setattr__(self, "opened_quantity", decimal_value(self.opened_quantity, "opened_quantity", positive=True))
        object.__setattr__(self, "remaining_quantity", decimal_value(self.remaining_quantity, "remaining_quantity", nonnegative=True))
        object.__setattr__(self, "acquisition_price", decimal_value(self.acquisition_price, "acquisition_price", positive=True))
        object.__setattr__(self, "opened_at", require_aware(self.opened_at, "opened_at"))
        object.__setattr__(self, "closures", immutable_metadata(self.closures))
        object.__setattr__(self, "realized_pnl", decimal_value(self.realized_pnl, "realized_pnl"))
        if self.mfe_price is not None:
            object.__setattr__(self, "mfe_price", decimal_value(self.mfe_price, "mfe_price", positive=True))
        if self.mae_price is not None:
            object.__setattr__(self, "mae_price", decimal_value(self.mae_price, "mae_price", positive=True))
        if self.remaining_quantity > self.opened_quantity:
            raise ValueError("remaining_quantity cannot exceed opened_quantity")

    @property
    def status(self) -> LotStatus:
        if self.remaining_quantity <= 0:
            return "closed"
        if self.remaining_quantity < self.opened_quantity:
            return "partially_closed"
        return "open"

    @property
    def signed_quantity(self) -> Decimal:
        return -self.remaining_quantity if self.position_side == "short" else self.remaining_quantity


def fold_price_extremum(
    position_side: PositionSide, mfe_price: Decimal | None, mae_price: Decimal | None, price: Decimal
) -> tuple[Decimal, Decimal]:
    """Direction-aware running price extremes for a PositionLot -- favorable
    = higher price for a long lot, lower for a short (mirrors
    monitor/coordinator.py::_breached's own long/short branching). Extends,
    never narrows, whatever was already recorded; initializes both to
    `price` when nothing existed yet. Shared by settlement/engine.py
    (folding each fill's own price) and monitor/coordinator.py (folding
    each cycle's broker-observed price) -- both write to PositionLot.mfe_price
    /mae_price, so this single function is the one place that logic lives."""
    if position_side == "long":
        new_mfe = price if mfe_price is None else max(mfe_price, price)
        new_mae = price if mae_price is None else min(mae_price, price)
    else:
        new_mfe = price if mfe_price is None else min(mfe_price, price)
        new_mae = price if mae_price is None else max(mae_price, price)
    return new_mfe, new_mae


@dataclass(frozen=True, slots=True)
class CashLedgerEntry:
    entry_id: str
    idempotency_key: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    reason: str

    def __post_init__(self) -> None:
        for name in ("entry_id", "idempotency_key", "currency", "reason"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "amount", decimal_value(self.amount, "amount"))
        object.__setattr__(self, "occurred_at", require_aware(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class PnlRecord:
    record_id: str
    asset: AssetIdentity
    realized: Decimal
    unrealized: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", require_text(self.record_id, "record_id"))
        object.__setattr__(self, "realized", decimal_value(self.realized, "realized"))
        object.__setattr__(self, "unrealized", decimal_value(self.unrealized, "unrealized"))
        object.__setattr__(self, "as_of", require_aware(self.as_of, "as_of"))
