from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .base import decimal_value, require_aware, require_text
from .enums import ExecutionMode, OrderStatus, Side
from .market import AssetIdentity


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    trade_intent_id: str
    idempotency_key: str
    asset: AssetIdentity
    side: Side
    execution_mode: ExecutionMode
    status: OrderStatus
    requested_quantity: Decimal
    submitted_at: datetime
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side) or not isinstance(self.execution_mode, ExecutionMode) or not isinstance(self.status, OrderStatus):
            raise TypeError("order enum fields must use canonical enums")
        for name in ("order_id", "trade_intent_id", "idempotency_key"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "requested_quantity", decimal_value(self.requested_quantity, "requested_quantity", positive=True))
        object.__setattr__(self, "filled_quantity", decimal_value(self.filled_quantity, "filled_quantity", nonnegative=True))
        object.__setattr__(self, "submitted_at", require_aware(self.submitted_at, "submitted_at"))
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("filled_quantity cannot exceed requested_quantity")
        if self.average_fill_price is not None:
            object.__setattr__(self, "average_fill_price", decimal_value(self.average_fill_price, "average_fill_price", positive=True))
