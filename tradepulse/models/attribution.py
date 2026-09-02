"""Per-round-trip trade outcome attribution -- pure, write-only observability.

Persists why a trade happened (entry provenance -- regime, confidence,
composite score/factor breakdown, sizing reasons, already scattered across
TradeIntent.risk_snapshot and Opportunity.metadata, never joined) and what
happened to it (exit reason, realized P&L, max favorable/adverse price
excursion during the holding period). Created once per (PositionLot,
closing fill) pair by settlement/engine.py::_project_attribution.

Nothing in risk/engine.py, execution/gateway.py, or scanner/coordinator.py's
candidate selection ever reads a TradeAttribution row -- this exists purely
so a later, separate phase can calibrate strategy weights against real
outcomes, not to change any decision this system makes today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping

from .base import decimal_value, immutable_metadata, require_aware, require_text
from .market import AssetIdentity

ExitReason = Literal["stop_loss", "target_price", "other"]
_EXIT_REASONS = ("stop_loss", "target_price", "other")


@dataclass(frozen=True, slots=True)
class TradeAttribution:
    attribution_id: str
    asset: AssetIdentity
    lot_id: str
    opening_trade_intent_id: str
    closing_trade_intent_id: str
    closing_fill_id: str
    quantity: Decimal
    entry_price: Decimal
    entry_at: datetime
    exit_price: Decimal
    exit_at: datetime
    realized_pnl: Decimal
    created_at: datetime
    # None (unknown) means the opening intent had no protective levels at
    # all to classify against -- distinct from "other" (a real exit that
    # matched neither the stop nor the target). See settlement/engine.py::
    # _infer_exit_reason.
    exit_reason: ExitReason | None = None
    max_favorable_excursion: Decimal | None = None
    max_adverse_excursion: Decimal | None = None
    # A namespaced, schema-free provenance bag -- {"risk_snapshot": ...,
    # "opportunity_metadata": ...} -- deliberately NOT flat-merged (the two
    # sources both independently carry a "stop_loss" key; namespacing avoids
    # any silent collision rather than relying on the values happening to
    # agree).
    entry_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("attribution_id", "lot_id", "opening_trade_intent_id", "closing_trade_intent_id", "closing_fill_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in ("quantity", "entry_price", "exit_price"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, positive=True))
        object.__setattr__(self, "realized_pnl", decimal_value(self.realized_pnl, "realized_pnl"))
        if self.max_favorable_excursion is not None:
            object.__setattr__(self, "max_favorable_excursion", decimal_value(self.max_favorable_excursion, "max_favorable_excursion", positive=True))
        if self.max_adverse_excursion is not None:
            object.__setattr__(self, "max_adverse_excursion", decimal_value(self.max_adverse_excursion, "max_adverse_excursion", positive=True))
        object.__setattr__(self, "entry_at", require_aware(self.entry_at, "entry_at"))
        object.__setattr__(self, "exit_at", require_aware(self.exit_at, "exit_at"))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        object.__setattr__(self, "entry_context", immutable_metadata(self.entry_context))
        if self.exit_reason is not None and self.exit_reason not in _EXIT_REASONS:
            raise ValueError(f"exit_reason must be one of {_EXIT_REASONS} or None, got {self.exit_reason!r}")
