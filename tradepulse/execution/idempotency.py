"""Idempotency key derivation, the shared in-flight-intent guard, and the
atomic per-asset execution reservation used by both the scanner and the
position monitor -- port of execution.ts::deriveIdempotencyKey plus
symbol-level coordination that didn't exist in the audited source.
"""

from __future__ import annotations

from tradepulse.models import AssetIdentity, Side, TradeIntentStatus
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, acquire_lock, release_lock

# Defined here (not in gateway.py, which imports from this module) to avoid a
# circular import -- gateway.py imports IN_FLIGHT_STATUSES from here instead.
IN_FLIGHT_STATUSES = frozenset(
    {TradeIntentStatus.SUBMITTED, TradeIntentStatus.ACCEPTED, TradeIntentStatus.PARTIALLY_FILLED, TradeIntentStatus.RISK_APPROVED}
)

# Renewed while held (see execution/gateway.py's pre-submission fence and
# scanner/monitor's run_with_lock_renewal wrapping) -- a floor for a single
# heartbeat interval, not a hard cap on how long execute_intent may run.
SYMBOL_LOCK_TTL_SECONDS = 45


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
    """Catches an asset already busy from an earlier, unrelated attempt --
    complementary to, not a replacement for, reserve_symbol_for_execution
    below. The scanner (BUY-only) and the position monitor (protective-exit-
    only) run concurrently and operate on opposite sides of a symbol, but a
    monitor-driven close and a scanner-driven reopen of the SAME symbol in
    the same tick are not economically independent just because they're
    opposite sides. Callers must hold the asset's execution reservation
    while calling this -- checking it alone, without the reservation, would
    reopen the exact check-then-submit race the reservation exists to
    close."""
    rows = await repositories.trade_intents.list_all(limit=1000)
    blocking = {status.value for status in IN_FLIGHT_STATUSES} | {TradeIntentStatus.SUBMISSION_UNKNOWN.value}
    symbol_upper = symbol.upper()
    return any(row["payload"]["asset"]["symbol"] == symbol_upper and row["status"] in blocking for row in rows)


def execution_lock_key(asset: AssetIdentity) -> str:
    """Canonical, asset-class-qualified lock key -- not a bare symbol
    string, so a ticker-shaped identifier can never collide across asset
    classes (crypto pairs already share ticker shapes with equities; more
    so once multi-asset scanning lands)."""
    return f"execution:{asset.asset_class.value}:{asset.symbol.upper()}"


async def reserve_symbol_for_execution(database: AsyncSQLiteDatabase, asset: AssetIdentity, owner_token: str) -> bool:
    """Atomic per-asset execution reservation -- the DB-enforced upgrade to
    has_in_flight_intent's former best-effort check-then-submit gap. Only
    one caller (scanner or monitor) can hold this per asset at a time; a
    concurrent second caller's acquire_lock call returns False immediately
    (not a blocking wait), so the loser cleanly skips this asset for this
    cycle instead of racing to submit."""
    return await acquire_lock(database, execution_lock_key(asset), owner_token, "execute_intent", SYMBOL_LOCK_TTL_SECONDS)


async def release_symbol_reservation(database: AsyncSQLiteDatabase, asset: AssetIdentity, owner_token: str) -> None:
    await release_lock(database, execution_lock_key(asset), owner_token)
