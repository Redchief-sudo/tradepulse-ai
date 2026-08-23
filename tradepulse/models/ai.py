from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .base import immutable_metadata, require_aware, require_text


@dataclass(frozen=True, slots=True)
class AIRequest:
    request_id: str
    correlation_id: str
    operation: str
    schema_version: str
    created_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("request_id", "correlation_id", "operation", "schema_version"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        object.__setattr__(self, "payload", immutable_metadata(self.payload))


@dataclass(frozen=True, slots=True)
class AIResponse:
    request_id: str
    provider: str
    model: str
    schema_version: str
    completed_at: datetime
    result: Mapping[str, Any]
    latency_ms: int

    def __post_init__(self) -> None:
        for name in ("request_id", "provider", "model", "schema_version"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "completed_at", require_aware(self.completed_at, "completed_at"))
        object.__setattr__(self, "result", immutable_metadata(self.result))
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be nonnegative")
