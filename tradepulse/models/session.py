from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .base import require_aware, require_text
from .enums import SessionState


@dataclass(frozen=True, slots=True)
class TradingSession:
    session_id: str
    state: SessionState
    trading_active: bool
    updated_at: datetime
    kill_switch_reason: str | None = None
    kill_switch_at: datetime | None = None
    kill_switch_reset_required: bool = False
    financial_integrity_reason: str | None = None
    financial_integrity_manual_reenable_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.state, SessionState):
            raise TypeError("state must be SessionState")
        object.__setattr__(self, "session_id", require_text(self.session_id, "session_id"))
        object.__setattr__(self, "updated_at", require_aware(self.updated_at, "updated_at"))
        if self.kill_switch_at is not None:
            object.__setattr__(self, "kill_switch_at", require_aware(self.kill_switch_at, "kill_switch_at"))

        if self.state == SessionState.RISK_STOPPED:
            if not self.kill_switch_reset_required:
                raise ValueError("risk_stopped session requires kill_switch_reset_required")
            if self.trading_active:
                raise ValueError("risk_stopped session cannot be trading_active")

        if self.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED:
            if not self.financial_integrity_manual_reenable_required:
                raise ValueError("financial_integrity_blocked session requires manual_reenable_required")
            if self.trading_active:
                raise ValueError("financial_integrity_blocked session cannot be trading_active")

        if self.state == SessionState.ACTIVE:
            if self.kill_switch_reset_required or self.financial_integrity_manual_reenable_required:
                raise ValueError("active session cannot carry an unresolved kill-switch or integrity block")

    def is_tradeable(self) -> bool:
        return self.state == SessionState.ACTIVE and self.trading_active
