from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

from .base import immutable_metadata, require_aware, require_text
from .enums import ReconciliationOutcome

ReconciliationType = Literal["order", "position"]
_TYPES = {"order", "position"}


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    record_id: str
    reconciliation_type: ReconciliationType
    subject_id: str
    outcome: ReconciliationOutcome
    expected: Mapping[str, Any]
    actual: Mapping[str, Any]
    occurred_at: datetime
    corrective_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReconciliationOutcome):
            raise TypeError("outcome must be ReconciliationOutcome")
        if self.reconciliation_type not in _TYPES:
            raise ValueError(f"reconciliation_type must be one of {sorted(_TYPES)}")
        for name in ("record_id", "subject_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "occurred_at", require_aware(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "expected", immutable_metadata(self.expected))
        object.__setattr__(self, "actual", immutable_metadata(self.actual))
        if self.outcome == ReconciliationOutcome.CORRECTED and not self.corrective_action:
            raise ValueError("corrected outcome requires corrective_action")
