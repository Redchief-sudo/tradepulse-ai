"""Settlement lifecycle constants and staged-checkpoint runner -- port of
base44/shared/settlementState.ts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from tradepulse.models import SettlementEvent, SettlementStatus

SETTLEMENT_STAGES: tuple[tuple[str, str], ...] = (
    ("lot_projected", "project_lot"),
    ("attribution_projected", "project_attribution"),
    ("cash_projected", "project_cash"),
    ("holding_projected", "project_holding"),
    ("trade_projected", "project_trade"),
    ("integrity_verified", "verify_integrity"),
)

MAX_SETTLEMENT_ATTEMPTS = 8
RETRY_BASE_SECONDS = 15
RETRY_MAX_SECONDS = 300


def retry_delay_seconds(attempt: int) -> int:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempt - 1))


@dataclass(frozen=True, slots=True)
class ClassifiedFailure:
    status: SettlementStatus
    attempt_count: int
    error: str
    next_retry_at: datetime | None


def classify_settlement_failure(attempt_count: int, error: Exception, now: datetime) -> ClassifiedFailure:
    attempts = attempt_count + 1
    message = str(error)
    integrity_blocked = message.startswith("INTEGRITY_VIOLATION")
    exhausted = attempts >= MAX_SETTLEMENT_ATTEMPTS
    status = (
        SettlementStatus.INTEGRITY_BLOCKED
        if integrity_blocked
        else SettlementStatus.TERMINAL_FAILED
        if exhausted
        else SettlementStatus.RETRYABLE_FAILED
    )
    next_retry_at = (
        now + timedelta(seconds=retry_delay_seconds(attempts)) if status == SettlementStatus.RETRYABLE_FAILED else None
    )
    return ClassifiedFailure(status=status, attempt_count=attempts, error=message, next_retry_at=next_retry_at)


def is_settlement_processable(event: SettlementEvent, now: datetime, stale_lease_seconds: int, force_retry: bool = False) -> bool:
    if event.status == SettlementStatus.PENDING:
        return True
    if event.status == SettlementStatus.RETRYABLE_FAILED:
        return force_retry or event.next_retry_at is None or event.next_retry_at <= now
    return (
        event.status == SettlementStatus.PROCESSING
        and event.processing_started_at is not None
        and (now - event.processing_started_at).total_seconds() > stale_lease_seconds
    )


StageHandler = Callable[[SettlementEvent], Awaitable[Mapping[str, Any] | None]]
Checkpoint = Callable[[SettlementEvent], Awaitable[None]]
BeforeStage = Callable[[str, SettlementEvent], Awaitable[None]]


async def run_settlement_stages(
    event: SettlementEvent,
    handlers: Mapping[str, StageHandler],
    checkpoint: Checkpoint,
    before_stage: BeforeStage | None = None,
) -> SettlementEvent:
    """Runs each stage in order, skipping ones already flagged done, and
    checkpoints (persists) the event after EACH stage -- a crash mid-run
    resumes at the next incomplete stage, never restarts from scratch."""
    state = event
    for flag, handler_name in SETTLEMENT_STAGES:
        if getattr(state, flag):
            continue
        if before_stage:
            await before_stage(flag, state)
        result = await handlers[handler_name](state)
        patch: dict[str, Any] = {flag: True, **(result or {})}
        state = replace(state, **patch)
        await checkpoint(state)
    return state
