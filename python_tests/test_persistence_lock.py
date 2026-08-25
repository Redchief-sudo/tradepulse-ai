import asyncio
import sqlite3

from tradepulse.persistence import AsyncSQLiteDatabase, acquire_lock, release_lock, renew_lock, run_with_lock_renewal


async def _database(tmp_path) -> AsyncSQLiteDatabase:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return database


async def _expires_at(database: AsyncSQLiteDatabase, lock_key: str) -> str | None:
    def op(connection: sqlite3.Connection) -> str | None:
        row = connection.execute("SELECT expires_at FROM locks WHERE lock_key=?", (lock_key,)).fetchone()
        return row["expires_at"] if row is not None else None

    return await database.run(op)


async def _reassign_owner(database: AsyncSQLiteDatabase, lock_key: str, new_owner_token: str) -> None:
    """Directly mutates the lock row's owner_token -- simulates a competing
    acquire_lock() legitimately taking over after a real expiry, without
    needing to orchestrate real wall-clock timing races in a test."""

    def op(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE locks SET owner_token=? WHERE lock_key=?", (new_owner_token, lock_key))

    await database.run(op, write=True)


async def test_second_caller_cannot_acquire_a_live_lease(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=60) is True
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is False


async def test_true_concurrency_race_exactly_one_wins(tmp_path) -> None:
    database = await _database(tmp_path)
    results = await asyncio.gather(
        acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=60),
        acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60),
    )
    assert sorted(results) == [False, True]


async def test_expired_lease_is_reclaimable(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=-1) is True
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is True


async def test_release_then_reacquire_by_another_owner(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=60) is True
    await release_lock(database, "scan", "owner-a")
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is True


async def test_release_does_not_clobber_a_lease_taken_over_after_expiry(tmp_path) -> None:
    """owner-a's lease already expired and owner-b legitimately took over --
    owner-a calling release() late must not delete owner-b's live lease."""
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=-1) is True
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is True

    await release_lock(database, "scan", "owner-a")

    assert await acquire_lock(database, "scan", "owner-c", "scan", ttl_seconds=60) is False


# ---- renew_lock -------------------------------------------------------------

async def test_renew_lock_extends_expiry_for_current_owner(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=60) is True
    before = await _expires_at(database, "scan")

    assert await renew_lock(database, "scan", "owner-a", ttl_seconds=120) is True

    after = await _expires_at(database, "scan")
    assert after > before


async def test_renew_lock_fails_for_non_owner_and_does_not_modify(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=60) is True
    before = await _expires_at(database, "scan")

    assert await renew_lock(database, "scan", "owner-b", ttl_seconds=120) is False

    assert await _expires_at(database, "scan") == before


async def test_renew_lock_fails_for_nonexistent_key(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await renew_lock(database, "does-not-exist", "owner-a", ttl_seconds=60) is False


async def test_renew_lock_fails_for_already_expired_lease_even_with_matching_owner(tmp_path) -> None:
    """Expired ownership cannot resurrect itself -- same owner_token alone
    is not sufficient once the lease has genuinely lapsed."""
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=-1) is True  # already expired

    assert await renew_lock(database, "scan", "owner-a", ttl_seconds=60) is False


async def test_renew_and_release_by_stale_owner_never_touch_a_new_owners_lease(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=-1) is True  # already expired
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is True  # legitimately reclaimed
    owner_b_expiry = await _expires_at(database, "scan")

    assert await renew_lock(database, "scan", "owner-a", ttl_seconds=999) is False
    await release_lock(database, "scan", "owner-a")

    assert await _expires_at(database, "scan") == owner_b_expiry  # untouched
    assert await acquire_lock(database, "scan", "owner-c", "scan", ttl_seconds=60) is False  # owner-b's lease still live


# ---- run_with_lock_renewal --------------------------------------------------

async def test_run_with_lock_renewal_keeps_lease_alive_past_its_original_ttl(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=3) is True

    async def work() -> str:
        await asyncio.sleep(3.5)  # outlives the original 3s TTL -- only renewal (every ~1s) keeps it alive
        return "done"

    result = await run_with_lock_renewal(database, "scan", "owner-a", ttl_seconds=3, work=work())

    assert result == "done"
    # Still held by owner-a (renewal extended it past its original 1s window) --
    # a competing acquire must still fail.
    assert await acquire_lock(database, "scan", "owner-b", "scan", ttl_seconds=60) is False


async def test_run_with_lock_renewal_calls_callback_once_on_lost_lease_but_work_still_completes(tmp_path) -> None:
    database = await _database(tmp_path)
    assert await acquire_lock(database, "scan", "owner-a", "scan", ttl_seconds=1) is True

    failures: list[None] = []

    async def on_renewal_failed() -> None:
        failures.append(None)

    async def work() -> str:
        await asyncio.sleep(0.3)
        await _reassign_owner(database, "scan", "owner-b")  # simulate a legitimate takeover after expiry
        await asyncio.sleep(1.0)  # long enough for the next heartbeat tick (interval=1s) to observe the theft
        return "done"

    result = await run_with_lock_renewal(database, "scan", "owner-a", ttl_seconds=1, work=work(), on_renewal_failed=on_renewal_failed)

    assert result == "done"  # never cancelled
    assert len(failures) == 1  # called exactly once, not repeatedly
