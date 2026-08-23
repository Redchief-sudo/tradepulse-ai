"""Database-enforced advisory lock -- application-level mutual exclusion for
work that must not run twice concurrently (e.g. a scan cycle), independent
of whatever external scheduling discipline (cron, systemd) invokes the
process. Uses the same BEGIN IMMEDIATE serialization as
RecordRepository.claim_if_processable: the read-check-write happens inside
one write transaction, so a concurrent second caller genuinely blocks until
the first commits, then correctly sees the lock as held (or expired).

A lease, not a mutex: an expired lock (owner crashed without releasing it)
is silently reclaimable by the next caller rather than requiring manual
intervention -- the same reclaim-a-stale-lease principle already used for
settlement processing (see settlement/stages.py::is_settlement_processable).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .database import AsyncSQLiteDatabase


async def acquire_lock(
    database: AsyncSQLiteDatabase, lock_key: str, owner_token: str, command: str, ttl_seconds: int
) -> bool:
    """Returns True if the caller now holds the lock (either it was free, or
    the previous holder's lease had expired), False if someone else
    currently holds a live lease."""
    now = datetime.now(UTC)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    now_iso = now.isoformat()

    def op(connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT expires_at FROM locks WHERE lock_key=?", (lock_key,)).fetchone()
        if row is not None and row["expires_at"] > now_iso:
            return False
        connection.execute(
            "INSERT INTO locks (lock_key, owner_token, acquired_at, expires_at, command) VALUES (?,?,?,?,?) "
            "ON CONFLICT(lock_key) DO UPDATE SET owner_token=excluded.owner_token, "
            "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at, command=excluded.command",
            (lock_key, owner_token, now_iso, expires_at, command),
        )
        return True

    return await database.run(op, write=True)


async def release_lock(database: AsyncSQLiteDatabase, lock_key: str, owner_token: str) -> None:
    """Only releases if this caller still owns the lease -- if it already
    expired and a different caller took it over, this must not delete
    theirs."""

    def op(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM locks WHERE lock_key=? AND owner_token=?", (lock_key, owner_token))

    await database.run(op, write=True)
