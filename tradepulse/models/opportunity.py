from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .base import immutable_metadata, require_aware, require_text
from .market import AssetIdentity, MarketQuote


@dataclass(frozen=True, slots=True)
class Opportunity:
    opportunity_id: str
    scan_generation: str
    asset: AssetIdentity
    quote: MarketQuote
    source: str
    created_at: datetime
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "opportunity_id", require_text(self.opportunity_id, "opportunity_id"))
        object.__setattr__(self, "scan_generation", require_text(self.scan_generation, "scan_generation"))
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        object.__setattr__(self, "metadata", immutable_metadata(self.metadata))
        if self.quote.asset != self.asset:
            raise ValueError("quote asset must match opportunity asset")
        if self.confidence is not None and not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
