from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .base import require_aware, require_text
from .enums import ScanRunStatus, ScanTrigger


@dataclass(frozen=True, slots=True)
class ScanRun:
    scan_run_id: str
    scan_generation: str
    trigger: ScanTrigger
    status: ScanRunStatus
    started_at: datetime
    lock_owner_token: str
    completed_at: datetime | None = None
    candidates_discovered: int = 0
    candidates_approved: int = 0
    orders_submitted: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, ScanTrigger):
            raise TypeError("trigger must be ScanTrigger")
        if not isinstance(self.status, ScanRunStatus):
            raise TypeError("status must be ScanRunStatus")
        for name in ("scan_run_id", "scan_generation", "lock_owner_token"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "started_at", require_aware(self.started_at, "started_at"))
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", require_aware(self.completed_at, "completed_at"))
        for name in ("candidates_discovered", "candidates_approved", "orders_submitted"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.status in (ScanRunStatus.COMPLETED, ScanRunStatus.FAILED) and self.completed_at is None:
            raise ValueError("terminal scan run requires completed_at")
        if self.status == ScanRunStatus.FAILED and not self.error:
            raise ValueError("failed scan run requires error")
