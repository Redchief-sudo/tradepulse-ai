import asyncio

from tradepulse.persistence import AsyncSQLiteDatabase, acquire_lock, release_lock


async def _database(tmp_path) -> AsyncSQLiteDatabase:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return database


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
