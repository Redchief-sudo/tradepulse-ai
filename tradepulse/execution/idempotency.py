"""Idempotency key derivation -- port of execution.ts::deriveIdempotencyKey."""

from __future__ import annotations

from tradepulse.models import Side


def derive_idempotency_key(
    strategy: str, decision_id: str | None, signal_timestamp: str | None, symbol: str, side: Side
) -> str | None:
    """Retried calls (a cron re-fire, a resumed process) must resume the same
    intent rather than submit a second broker order. If we have enough
    signal identity (a decision_id or signal_timestamp), derive a stable
    key; otherwise return None -- a new intent will be created (this is only
    safe for genuinely one-off, caller-deduplicated calls)."""
    if decision_id or signal_timestamp:
        return f"ik-{strategy}-{decision_id or signal_timestamp}-{symbol.upper()}-{side.value}"
    return None
