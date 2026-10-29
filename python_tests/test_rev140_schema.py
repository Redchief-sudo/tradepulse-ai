"""Rev.140 task 2: append-only quarantine tables, verified triggers, records."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradepulse.models import AssetClass, AssetIdentity
from tradepulse.models.base import DomainValidationError
from tradepulse.models.quarantine import ShortExposureLiability
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.persistence.database import QUARANTINE_TABLES, DatabaseError, verify_schema_triggers

AT = datetime(2026, 10, 28, 15, tzinfo=UTC)
AAPL = AssetIdentity(asset_class=AssetClass.EQUITY, symbol="AAPL", native_asset_id="alpaca:AAPL")
UNIQUE = {
    "short_exposure_liabilities": "fill_id", "unmanaged_long_inventory": "cover_activity_id",
    "quarantine_resolutions": "resolution_key", "quarantine_fee_allocations": "allocation_key",
    "quarantine_fee_recognitions": "recognition_key", "legacy_lot_conversions": "lot_id",
    "fill_cursor_bootstrap": "account_ref",
}


async def _db(path: Path) -> AsyncSQLiteDatabase:
    database = AsyncSQLiteDatabase(f"sqlite:///{path}")
    await database.initialize()
    return database


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _insert(connection: sqlite3.Connection, table: str, record_id: str = "r1") -> None:
    columns, values = ["record_id", "payload", "created_at"], [record_id, "{}", AT.isoformat()]
    if table in UNIQUE:
        columns.append(UNIQUE[table])
        values.append(f"u-{record_id}")
    connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(values))})", values)


def _liability(**changes: object) -> ShortExposureLiability:
    fields: dict[str, object] = {
        "liability_id": "liability:f1", "fill_id": "f1", "broker_order_id": "o1", "trade_intent_id": "i1",
        "asset": AAPL, "quantity": Decimal(2), "sale_price": Decimal(100), "multiplier": Decimal(1),
        "proceeds": Decimal(200), "opened_at": AT}
    fields.update(changes)
    return ShortExposureLiability(**fields)  # type: ignore[arg-type]


async def test_ten_tables_exist(tmp_path: Path) -> None:
    await _db(tmp_path / "t.db")
    with _connect(tmp_path / "t.db") as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert len(QUARANTINE_TABLES) == 10
    assert set(QUARANTINE_TABLES) <= names
    assert set(QUARANTINE_TABLES) <= set(PersistenceRepositories.__dataclass_fields__)


@pytest.mark.parametrize("table", QUARANTINE_TABLES)
@pytest.mark.parametrize("statement", ["UPDATE {t} SET payload='x'", "DELETE FROM {t}"])
async def test_update_and_delete_refused_on_every_table(tmp_path: Path, table: str, statement: str) -> None:
    await _db(tmp_path / "t.db")
    connection = _connect(tmp_path / "t.db")
    try:
        _insert(connection, table)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement.format(t=table))
        assert connection.execute(f"SELECT payload FROM {table}").fetchall() == [("{}",)]
    finally:
        connection.close()


async def test_missing_trigger_refused_at_initialize(tmp_path: Path) -> None:
    database = await _db(tmp_path / "t.db")
    with _connect(tmp_path / "t.db") as connection:
        connection.execute("DROP TRIGGER quarantine_no_update_quarantine_executions")
    with pytest.raises(DatabaseError, match="SCHEMA_TRIGGER_MISSING:quarantine_no_update_quarantine_executions"):
        await database.initialize()


async def test_altered_trigger_refused_at_initialize(tmp_path: Path) -> None:
    database = await _db(tmp_path / "t.db")
    with _connect(tmp_path / "t.db") as connection:
        connection.executescript("""
            DROP TRIGGER quarantine_no_delete_fill_cursor_bootstrap;
            CREATE TRIGGER quarantine_no_delete_fill_cursor_bootstrap BEFORE DELETE ON fill_cursor_bootstrap
            WHEN 0 BEGIN SELECT RAISE(ABORT, 'fill_cursor_bootstrap is append-only'); END;""")
    with pytest.raises(DatabaseError, match="SCHEMA_TRIGGER_MISSING:quarantine_no_delete_fill_cursor_bootstrap"):
        await database.initialize()


async def test_backup_restore_keeps_triggers(tmp_path: Path) -> None:
    await _db(tmp_path / "t.db")
    source, target = _connect(tmp_path / "t.db"), _connect(tmp_path / "restored.db")
    try:
        source.backup(target)
        verify_schema_triggers(target)
    finally:
        source.close()
        target.close()
    await (AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'restored.db'}")).initialize()


async def test_new_tables_mark_ever_evidence(tmp_path: Path) -> None:
    await _db(tmp_path / "t.db")
    with _connect(tmp_path / "t.db") as connection:
        assert connection.execute("SELECT ever_evidence FROM verification_identity").fetchone() == (0,)
        _insert(connection, "short_exposure_liabilities")
        assert connection.execute("SELECT ever_evidence FROM verification_identity").fetchone() == (1,)


@pytest.mark.parametrize("change", [
    {"quantity": 2.0}, {"quantity": Decimal(0)}, {"quantity": Decimal(-1)}, {"quantity": True},
    {"sale_price": 100.0}, {"proceeds": Decimal("NaN")}, {"multiplier": Decimal(0)},
    {"opened_at": datetime(2026, 10, 28, 15)}, {"fill_id": " "},  # noqa: DTZ001 - a naive time must be refused
])
def test_models_reject_float_and_nonpositive(change: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        _liability(**change)


async def test_records_round_trip_through_the_repository(tmp_path: Path) -> None:
    from tradepulse.persistence import hydrate

    repositories = PersistenceRepositories.create(await _db(tmp_path / "t.db"))
    liability = _liability()
    assert await repositories.short_exposure_liabilities.create_once(liability.liability_id, liability,
                                                                    unique_value=liability.fill_id)
    assert not await repositories.short_exposure_liabilities.create_once(liability.liability_id, liability,
                                                                        unique_value=liability.fill_id)
    row = await repositories.short_exposure_liabilities.get(liability.liability_id)
    assert row is not None and hydrate("short_exposure_liabilities", row["payload"]) == liability


async def test_existing_database_gains_tables_without_change(tmp_path: Path) -> None:
    """A database from before Rev.140: initialize adds the ten tables and
    their triggers and leaves every existing row byte-identical."""
    path = tmp_path / "old.db"
    await _db(path)
    with _connect(path) as connection:
        for table in QUARANTINE_TABLES:
            connection.execute(f"DROP TABLE {table}")
        connection.execute("INSERT INTO cash_ledger (record_id,idempotency_key,payload,created_at) VALUES "
                           "('c1','fill:cash:a1','{\"amount\":\"-100.00\"}','2026-10-01T00:00:00+00:00')")
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        before = {t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()
    with _connect(path) as connection:
        after = {t: connection.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
        verify_schema_triggers(connection)
    assert after == before
