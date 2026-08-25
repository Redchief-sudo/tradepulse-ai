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

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from .database import AsyncSQLiteDatabase

T = TypeVar("T")


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


async def renew_lock(database: AsyncSQLiteDatabase, lock_key: str, owner_token: str, ttl_seconds: int) -> bool:
    """Extends expires_at ONLY if this caller still owns the lease AND that
    lease is still unexpired -- same-owner alone isn't enough: a heartbeat
    that fires late (after the lease already lapsed, before anyone
    reclaimed it) must not resurrect an already-lost lease with ambiguous
    ownership. Returns False if the lease was lost (expired, or reclaimed
    by someone else, or never held) -- the caller must treat that as a
    signal its exclusivity may be gone."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    def op(connection: sqlite3.Connection) -> bool:
        cursor = connection.execute(
            "UPDATE locks SET expires_at=? WHERE lock_key=? AND owner_token=? AND expires_at > ?",
            (expires_at, lock_key, owner_token, now_iso),
        )
        return cursor.rowcount == 1

    return await database.run(op, write=True)


async def run_with_lock_renewal(
    database: AsyncSQLiteDatabase, lock_key: str, owner_token: str, ttl_seconds: int,
    work: Coroutine[Any, Any, T], *, on_renewal_failed: Callable[[], Awaitable[None]] | None = None,
) -> T:
    """Runs `work` while a background task renews the lease every
    ttl_seconds/3 -- so work that legitimately takes longer than the TTL
    never gets its lease stolen by a concurrent invocation. If a renewal
    ever fails (lease lost), calls on_renewal_failed once (if given) and
    stops renewing -- but does NOT cancel `work` itself; aborting mid-flight
    (a live broker order being polled, a settlement mid-write) could leave
    things in a worse state than letting it finish. The caller's callback
    is where "stop starting new work" gets signaled -- this function is
    deliberately alert-agnostic (no TelegramAlerter dependency here), just
    a generic hook."""
    stop = asyncio.Event()

    async def heartbeat() -> None:
        interval = max(ttl_seconds / 3, 1)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return  # stop was set -- work finished
            except asyncio.TimeoutError:
                pass
            if not await renew_lock(database, lock_key, owner_token, ttl_seconds):
                if on_renewal_failed is not None:
                    await on_renewal_failed()
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        return await work
    finally:
        stop.set()
        await heartbeat_task
