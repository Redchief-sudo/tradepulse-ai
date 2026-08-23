from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

from .base import immutable_metadata, require_aware, require_text

Severity = Literal["info", "warning", "error", "critical"]
_SEVERITIES = {"info", "warning", "error", "critical"}


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: str
    severity: Severity
    message: str
    occurred_at: datetime
    correlation_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "message"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        if self.severity not in _SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_SEVERITIES)}")
        object.__setattr__(self, "occurred_at", require_aware(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "details", immutable_metadata(self.details))
