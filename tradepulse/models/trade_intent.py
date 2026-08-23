from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .base import decimal_value, immutable_metadata, require_aware, require_text
from .enums import ExecutionMode, Side, TradeIntentStatus
from .market import AssetIdentity


@dataclass(frozen=True, slots=True)
class TradeIntent:
    trade_intent_id: str
    idempotency_key: str
    correlation_id: str
    asset: AssetIdentity
    side: Side
    execution_mode: ExecutionMode
    strategy: str
    created_at: datetime
    requested_quantity: Decimal | None = None
    requested_notional: Decimal | None = None
    reference_price: Decimal | None = None
    confidence: float | None = None
    risk_snapshot: Mapping[str, Any] = field(default_factory=dict)
    status: TradeIntentStatus = TradeIntentStatus.PROPOSED
    filled_quantity: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None
    broker_order_id: str | None = None
    client_order_id: str | None = None
    reserved_cash: Decimal | None = None
    consumed_cash: Decimal | None = None
    realized_pnl: Decimal | None = None
    rejection_reason: str | None = None
    sector: str | None = None
    stop_loss: Decimal | None = None
    target_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side) or not isinstance(self.execution_mode, ExecutionMode):
            raise TypeError("side and execution_mode must use canonical enums")
        if not isinstance(self.status, TradeIntentStatus):
            raise TypeError("status must be TradeIntentStatus")
        for name in ("trade_intent_id", "idempotency_key", "correlation_id", "strategy"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        object.__setattr__(self, "risk_snapshot", immutable_metadata(self.risk_snapshot))
        if self.requested_quantity is None and self.requested_notional is None:
            raise ValueError("requested_quantity or requested_notional is required")
        if self.requested_quantity is not None:
            object.__setattr__(self, "requested_quantity", decimal_value(self.requested_quantity, "requested_quantity", positive=True))
        if self.requested_notional is not None:
            object.__setattr__(self, "requested_notional", decimal_value(self.requested_notional, "requested_notional", positive=True))
        if self.reference_price is not None:
            object.__setattr__(self, "reference_price", decimal_value(self.reference_price, "reference_price", positive=True))
        if self.confidence is not None and not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        object.__setattr__(self, "filled_quantity", decimal_value(self.filled_quantity, "filled_quantity", nonnegative=True))
        if self.filled_avg_price is not None:
            object.__setattr__(self, "filled_avg_price", decimal_value(self.filled_avg_price, "filled_avg_price", positive=True))
        if self.reserved_cash is not None:
            object.__setattr__(self, "reserved_cash", decimal_value(self.reserved_cash, "reserved_cash", nonnegative=True))
        if self.consumed_cash is not None:
            object.__setattr__(self, "consumed_cash", decimal_value(self.consumed_cash, "consumed_cash", nonnegative=True))
        if self.realized_pnl is not None:
            object.__setattr__(self, "realized_pnl", decimal_value(self.realized_pnl, "realized_pnl"))
        if self.stop_loss is not None:
            object.__setattr__(self, "stop_loss", decimal_value(self.stop_loss, "stop_loss", positive=True))
        if self.target_price is not None:
            object.__setattr__(self, "target_price", decimal_value(self.target_price, "target_price", positive=True))
