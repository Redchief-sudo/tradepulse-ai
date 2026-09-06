from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .base import decimal_value, require_aware, require_text
from .enums import ExecutionMode, IntegrityHoldType, SettlementStatus, Side
from .market import AssetIdentity


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """One fill's journey through the single-writer settlement pipeline --
    port of the Base44 SettlementEvent entity, carrying the staged-projection
    checkpoint flags (lot/attribution/cash/holding/trade_projected,
    integrity_verified) directly so a crash mid-projection resumes at the
    next incomplete stage rather than restarting
    (settlement/stages.py::run_settlement_stages).

    `trade_projected` covers updating the originating TradeIntent's
    cumulative fill/realized-pnl summary -- there is no separate "Trade"
    entity in this system (TradeIntent already carries that summary).
    """

    settlement_event_id: str
    fill_id: str
    trade_intent_id: str
    asset: AssetIdentity
    side: Side
    execution_mode: ExecutionMode
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    status: SettlementStatus = SettlementStatus.PENDING
    fees: Decimal = Decimal("0")
    sector: str | None = None
    broker_order_id: str | None = None
    broker_fill_id: str | None = None
    client_order_id: str | None = None
    lot_projected: bool = False
    attribution_projected: bool = False
    cash_projected: bool = False
    holding_projected: bool = False
    trade_projected: bool = False
    integrity_verified: bool = False
    realized_pnl: Decimal | None = None
    attempt_count: int = 0
    error_code: str | None = None
    next_retry_at: datetime | None = None
    processing_owner: str | None = None
    processing_started_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SettlementStatus):
            raise TypeError("status must be SettlementStatus")
        if not isinstance(self.side, Side) or not isinstance(self.execution_mode, ExecutionMode):
            raise TypeError("side and execution_mode must use canonical enums")
        for name in ("settlement_event_id", "fill_id", "trade_intent_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "quantity", decimal_value(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", decimal_value(self.price, "price", positive=True))
        object.__setattr__(self, "fees", decimal_value(self.fees, "fees", nonnegative=True))
        object.__setattr__(self, "occurred_at", require_aware(self.occurred_at, "occurred_at"))
        if self.next_retry_at is not None:
            object.__setattr__(self, "next_retry_at", require_aware(self.next_retry_at, "next_retry_at"))
        if self.processing_started_at is not None:
            object.__setattr__(self, "processing_started_at", require_aware(self.processing_started_at, "processing_started_at"))
        if self.realized_pnl is not None:
            object.__setattr__(self, "realized_pnl", decimal_value(self.realized_pnl, "realized_pnl"))
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be nonnegative")
        if self.status == SettlementStatus.COMPLETED and not self.integrity_verified:
            raise ValueError("completed settlement must be integrity_verified")


@dataclass(frozen=True, slots=True)
class IntegrityHold:
    """FIN-095-02: persisted per-order integrity hold, keyed by
    broker_order_id (record_id in the integrity_holds table) -- see
    IntegrityHoldType for the two-state lifecycle. Checked atomically
    (same SQLite transaction) by settlement's guarded writes to
    position_lots/holdings/trade_intents via
    persistence/repositories.py::_check_integrity_hold, so a dispute
    proven concurrently with an in-flight settlement stage can never be
    interleaved with that stage's own write -- one transaction always
    commits strictly before the other.

    attempt_count/next_retry_at mirror SettlementEvent's own retry fields
    -- used by reconciliation's re-verification step (not settlement's own
    retry, which only retries PROJECTION, never re-verification) to avoid
    hammering a persistently-unavailable broker every reconciliation
    cycle."""

    broker_order_id: str
    trade_intent_id: str
    hold_type: IntegrityHoldType
    reason: str
    created_at: datetime
    attempt_count: int = 0
    next_retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.hold_type, IntegrityHoldType):
            raise TypeError("hold_type must be IntegrityHoldType")
        for name in ("broker_order_id", "trade_intent_id", "reason"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        if self.next_retry_at is not None:
            object.__setattr__(self, "next_retry_at", require_aware(self.next_retry_at, "next_retry_at"))
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be nonnegative")
