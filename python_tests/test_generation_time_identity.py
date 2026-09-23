"""Mandatory timestamp and non-resettable database provenance regressions."""
from datetime import UTC, datetime, timedelta, timezone
import sqlite3

import pytest

from tradepulse.persistence import AsyncSQLiteDatabase, DatabaseError
from tradepulse.time import aware_utc


@pytest.mark.parametrize("value", [None, "", "garbage", "2026-09-22", "2026-09-22T09:30:00", datetime(2026, 9, 22)])
def test_mandatory_time_rejects_missing_malformed_or_naive(value):
    with pytest.raises(ValueError, match="mandatory aware timestamp"):
        aware_utc(value)


@pytest.mark.parametrize("value", [
    "2026-09-22T09:30:00-07:00",
    datetime(2026, 9, 22, 9, 30, tzinfo=timezone(timedelta(hours=-7))),
])
def test_offset_normalization_preserves_instant(value):
    assert aware_utc(value) == datetime(2026, 9, 22, 16, 30, tzinfo=UTC)


async def test_evidence_history_survives_deletion_and_restart(tmp_path):
    db = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'fresh.db'}")
    await db.initialize()
    initial = await db.run(lambda c: dict(c.execute("SELECT * FROM verification_identity").fetchone()))
    assert initial["ever_evidence"] == initial["legacy_database"] == 0
    await db.run(lambda c: c.execute("INSERT INTO pnl_records VALUES ('p','{}','2026-09-22T00:00:00+00:00')"), write=True)
    await db.run(lambda c: c.execute("DELETE FROM pnl_records"), write=True)
    await db.initialize()
    identity = await db.run(lambda c: dict(c.execute("SELECT * FROM verification_identity").fetchone()))
    assert identity["database_id"] == initial["database_id"]
    assert identity["ever_evidence"] == 1
    with pytest.raises(DatabaseError, match="cannot be reset"):
        await db.run(lambda c: c.execute("UPDATE verification_identity SET ever_evidence=0"), write=True)
    with pytest.raises(DatabaseError, match="cannot be deleted"):
        await db.run(lambda c: c.execute("DELETE FROM verification_identity"), write=True)


async def test_old_empty_database_cannot_claim_fresh_provenance(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old_evidence (id TEXT)")
    db = AsyncSQLiteDatabase(f"sqlite:///{path}")
    await db.initialize()
    identity = await db.run(lambda c: dict(c.execute("SELECT * FROM verification_identity").fetchone()))
    assert identity["legacy_database"] == identity["ever_evidence"] == 1


async def test_generation_binding_cannot_be_reassigned(tmp_path):
    db = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'fresh.db'}")
    await db.initialize()
    await db.run(lambda c: c.execute("UPDATE verification_identity SET generation_id='one',checkpoint_id='cp',manifest_digest='digest'"), write=True)
    with pytest.raises(DatabaseError, match="cannot be reset or rebound"):
        await db.run(lambda c: c.execute("UPDATE verification_identity SET generation_id='two'"), write=True)
