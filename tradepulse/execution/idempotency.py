"""Idempotency key derivation, and the shared in-flight-intent guard used by
both the scanner and the position monitor -- port of
execution.ts::deriveIdempotencyKey plus a symbol-level coordination check
that didn't exist in the audited source.
"""

from __future__ import annotations

from tradepulse.models import Side, TradeIntentStatus
from tradepulse.persistence import PersistenceRepositories

# Defined here (not in gateway.py, which imports from this module) to avoid a
# circular import -- gateway.py imports IN_FLIGHT_STATUSES from here instead.
IN_FLIGHT_STATUSES = frozenset(
    {TradeIntentStatus.SUBMITTED, TradeIntentStatus.ACCEPTED, TradeIntentStatus.PARTIALLY_FILLED, TradeIntentStatus.RISK_APPROVED}
)


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


async def has_in_flight_intent(repositories: PersistenceRepositories, symbol: str) -> bool:
    """Best-effort guard, not an atomic barrier: the scanner (BUY-only) and
    the position monitor (protective-exit-only) run concurrently and operate
    on opposite sides of a symbol, but a monitor-driven close and a
    scanner-driven reopen of the SAME symbol in the same tick are not
    economically independent just because they're opposite sides. Checking
    for any non-terminal TradeIntent on this symbol before either coordinator
    submits a new one avoids the obviously-wasteful close-then-immediately-
    reopen case. There's still a narrow check-then-submit race since this
    isn't a DB-enforced lock -- an acceptable residual for what this guard is
    for; a hard per-symbol lock would be more machinery than this specific
    risk currently justifies."""
    rows = await repositories.trade_intents.list_all(limit=1000)
    blocking = {status.value for status in IN_FLIGHT_STATUSES} | {TradeIntentStatus.SUBMISSION_UNKNOWN.value}
    symbol_upper = symbol.upper()
    return any(row["payload"]["asset"]["symbol"] == symbol_upper and row["status"] in blocking for row in rows)
