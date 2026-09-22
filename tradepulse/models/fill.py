from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .base import decimal_value, require_aware, require_text
from .enums import ExecutionMode, Side
from .market import AssetIdentity


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    trade_intent_id: str
    order_id: str
    asset: AssetIdentity
    side: Side
    execution_mode: ExecutionMode
    quantity: Decimal
    price: Decimal
    fees: Decimal
    slippage: Decimal
    filled_at: datetime
    broker_fill_id: str | None = None
    reference_price: Decimal | None = None
    reference_bid: Decimal | None = None
    reference_ask: Decimal | None = None
    reference_observed_at: datetime | None = None
    submitted_at: datetime | None = None
    order_type: str | None = None
    fee_currency: str | None = None
    fee_source: str = "unavailable"

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side) or not isinstance(self.execution_mode, ExecutionMode):
            raise TypeError("fill enum fields must use canonical enums")
        for name in ("fill_id", "trade_intent_id", "order_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "quantity", decimal_value(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", decimal_value(self.price, "price", positive=True))
        object.__setattr__(self, "fees", decimal_value(self.fees, "fees", nonnegative=True))
        object.__setattr__(self, "slippage", decimal_value(self.slippage, "slippage", nonnegative=True))
        object.__setattr__(self, "filled_at", require_aware(self.filled_at, "filled_at"))
        for name in ("reference_price", "reference_bid", "reference_ask"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value, name, positive=True))
        for name in ("reference_observed_at", "submitted_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_aware(value, name))
        if self.order_type is not None:
            object.__setattr__(self, "order_type", require_text(self.order_type, "order_type"))
        if self.fee_currency is not None:
            object.__setattr__(self, "fee_currency", require_text(self.fee_currency, "fee_currency").upper())
        object.__setattr__(self, "fee_source", require_text(self.fee_source, "fee_source"))
        if self.reference_bid is not None and self.reference_ask is not None and self.reference_ask < self.reference_bid:
            raise ValueError("reference_ask cannot be below reference_bid")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price
