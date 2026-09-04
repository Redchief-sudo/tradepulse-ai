from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .base import immutable_metadata, require_aware, require_text
from .enums import AssetClass, ScanRunStatus, ScanTrigger


@dataclass(frozen=True, slots=True)
class ScanRun:
    scan_run_id: str
    scan_generation: str
    trigger: ScanTrigger
    asset_class: AssetClass
    status: ScanRunStatus
    started_at: datetime
    lock_owner_token: str
    completed_at: datetime | None = None
    candidates_discovered: int = 0
    candidates_approved: int = 0
    orders_submitted: int = 0
    error: str | None = None
    # Resolved Alpaca market-data capability for this cycle (see
    # providers/market_data_capability.py) -- optional/defaulting to None
    # so no backfill is needed for existing rows. Lets a durable reader
    # (e.g. a dashboard) see "what feed was actually used" without ever
    # probing Alpaca itself.
    market_data_tier: str | None = None
    equity_feed: str | None = None
    option_feed: str | None = None
    # Observability-only additions (dashboard Scanner Activity panel) --
    # optional/defaulting so no backfill is needed for existing rows, same
    # treatment as market_data_tier/equity_feed/option_feed above.
    # universe_size: len(lane_symbols) actually offered to the AI this
    # cycle -- the CONFIGURED universe, never to be confused with
    # candidates_discovered (what the AI returned) or candidates_approved
    # (what cleared the full gate chain). ai_response_request_id: links to
    # the AIResponse row already durably persisted for this cycle (see
    # scanner/coordinator.py) -- None whenever no AI response was ever
    # obtained for this run (e.g. SESSION_BLOCKED before the AI call, or
    # a legacy row predating this field).
    universe_size: int = 0
    ai_response_request_id: str | None = None
    # Market Regime Phase 2 provenance -- additive, optional/defaulting so
    # no backfill is needed for existing rows, same treatment as every
    # field above. `regime` is one of the 5 classified regime labels OR
    # the literal "unavailable" (benchmark fetch/classification failed
    # this cycle -- see scanner/coordinator.py::_classify_lane_regime);
    # `regime_reason` is only ever set alongside "unavailable" (e.g.
    # "benchmark_fetch_failed"/"benchmark_data_invalid"/
    # "regime_classification_failed"). Deliberately a loose `str`, not the
    # strict Regime Literal type, so it can hold "unavailable" too.
    regime: str | None = None
    regime_reason: str | None = None
    regime_confidence: int | None = None
    regime_position_multiplier: Decimal | None = None
    regime_realized_vol: Decimal | None = None
    # Strategy Sophistication Phase 1 -- which regime-conditioned weight
    # profile (see config/strategy_weights.py::regime_conditioned_weights)
    # was in effect for this scan cycle, e.g. "v1+regime:low_vol_bull".
    # Purely observational/additive, same treatment as every field above.
    regime_weight_profile: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, ScanTrigger):
            raise TypeError("trigger must be ScanTrigger")
        if not isinstance(self.asset_class, AssetClass):
            raise TypeError("asset_class must be AssetClass")
        if not isinstance(self.status, ScanRunStatus):
            raise TypeError("status must be ScanRunStatus")
        for name in ("scan_run_id", "scan_generation", "lock_owner_token"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "started_at", require_aware(self.started_at, "started_at"))
        if self.completed_at is not None:
            object.__setattr__(self, "completed_at", require_aware(self.completed_at, "completed_at"))
        for name in ("candidates_discovered", "candidates_approved", "orders_submitted", "universe_size"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.status in (ScanRunStatus.COMPLETED, ScanRunStatus.FAILED) and self.completed_at is None:
            raise ValueError("terminal scan run requires completed_at")
        if self.status == ScanRunStatus.FAILED and not self.error:
            raise ValueError("failed scan run requires error")


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """One row per candidate filtered out of a scan cycle (see
    scanner/coordinator.py::_reject) -- durable counterpart to what was
    previously only a stdout log line, so rejections can be reviewed after
    the fact instead of disappearing with the process's own output."""

    rejection_id: str
    scan_run_id: str
    scan_generation: str
    symbol: str
    asset_class: AssetClass
    reason: str
    occurred_at: datetime
    # Whatever _reject's own **context carried for this rejection (e.g.
    # confidence/min_confidence, ai_recommendation/deterministic_signal/
    # composite_score, error) -- free-form by design, since each of the ~19
    # reason codes carries different diagnostic fields.
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.asset_class, AssetClass):
            raise TypeError("asset_class must be AssetClass")
        for name in ("rejection_id", "scan_run_id", "scan_generation", "symbol", "reason"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(self, "occurred_at", require_aware(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "context", immutable_metadata(self.context))
