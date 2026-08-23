from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .codec import decode_payload, encode_payload
from .database import AsyncSQLiteDatabase
from .hydration import hydrate


TABLES = {
    "opportunities", "trade_intents", "orders", "fills", "settlements", "holdings",
    "position_lots", "cash_ledger", "pnl_records", "reconciliation_records",
    "trading_sessions", "audit_events", "scan_runs", "equity_snapshots",
}
STATUS_TABLES = {"trade_intents", "orders", "settlements", "trading_sessions", "scan_runs"}
UNIQUE_FIELDS = {
    "trade_intents": "idempotency_key",
    "orders": "idempotency_key",
    "fills": "broker_fill_id",
    "settlements": "fill_id",
    "position_lots": "originating_fill_id",
    "cash_ledger": "idempotency_key",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RecordRepository:
    def __init__(self, database: AsyncSQLiteDatabase, table: str) -> None:
        if table not in TABLES:
            raise ValueError(f"Unsupported repository table: {table}")
        self.database = database
        self.table = table

    async def create_once(
        self,
        record_id: str,
        payload: Any,
        *,
        status: str | None = None,
        unique_value: str | None = None,
    ) -> bool:
        if not record_id:
            raise ValueError("record_id is required")
        now = utc_now()
        columns = ["record_id"]
        values: list[Any] = [record_id]
        if self.table in STATUS_TABLES:
            if not status:
                raise ValueError(f"status is required for {self.table}")
            columns.append("status")
            values.append(status)
        unique_field = UNIQUE_FIELDS.get(self.table)
        if unique_field:
            if unique_value is None and self.table != "fills":
                raise ValueError(f"unique_value ({unique_field}) is required for {self.table}")
            columns.append(unique_field)
            values.append(unique_value)
        columns.extend(["payload", "created_at"])
        values.extend([encode_payload(payload), now])
        if self.table in STATUS_TABLES or self.table in {"holdings", "position_lots"}:
            columns.append("updated_at")
            values.append(now)
        placeholders = ",".join("?" for _ in values)
        sql = f"INSERT OR IGNORE INTO {self.table} ({','.join(columns)}) VALUES ({placeholders})"

        def insert(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(sql, values)
            return cursor.rowcount == 1

        return await self.database.run(insert, write=True)

    async def get(self, record_id: str) -> Mapping[str, Any] | None:
        def select(connection: sqlite3.Connection) -> Mapping[str, Any] | None:
            row = connection.execute(f"SELECT * FROM {self.table} WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = decode_payload(result["payload"])
            return result

        return await self.database.run(select)

    async def find_by_unique(self, unique_value: str) -> Mapping[str, Any] | None:
        field = UNIQUE_FIELDS.get(self.table)
        if not field:
            raise ValueError(f"{self.table} has no configured unique field")

        def select(connection: sqlite3.Connection) -> Mapping[str, Any] | None:
            row = connection.execute(f"SELECT * FROM {self.table} WHERE {field}=?", (unique_value,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = decode_payload(result["payload"])
            return result

        return await self.database.run(select)

    async def update(self, record_id: str, payload: Any, *, status: str | None = None) -> bool:
        now = utc_now()
        if self.table in STATUS_TABLES:
            if status is None:
                raise ValueError(f"status is required for {self.table}")
            sql = f"UPDATE {self.table} SET payload=?, status=?, updated_at=? WHERE record_id=?"
            values = (encode_payload(payload), status, now, record_id)
        elif self.table in {"holdings", "position_lots"}:
            sql = f"UPDATE {self.table} SET payload=?, updated_at=? WHERE record_id=?"
            values = (encode_payload(payload), now, record_id)
        else:
            raise ValueError(f"immutable repository cannot update: {self.table}")

        def execute(connection: sqlite3.Connection) -> bool:
            return connection.execute(sql, values).rowcount == 1

        return await self.database.run(execute, write=True)

    async def delete(self, record_id: str) -> bool:
        """Hard delete, restricted to `holdings` -- a current-state
        materialized view where a fully-closed position must not linger as a
        stale row. Every other table is append-only audit trail (fills,
        settlements, audit_events, etc.), matching the source system's own
        practice."""
        if self.table != "holdings":
            raise ValueError(f"delete is not permitted on {self.table} -- only holdings is a current-state view; all other tables are append-only audit trail")

        def execute(connection: sqlite3.Connection) -> bool:
            return connection.execute(f"DELETE FROM {self.table} WHERE record_id=?", (record_id,)).rowcount == 1

        return await self.database.run(execute, write=True)

    async def claim_if_processable(self, record_id: str, decide: Callable[[Any], Any | None]) -> Any | None:
        """Atomic read-decide-write. Within ONE write transaction: re-reads
        and hydrates the row, calls `decide(current_typed_value)`. If
        `decide` returns a new value (a model instance with a `.status`
        enum attribute), BOTH the payload blob and the status column are
        written together in a single UPDATE and the new value is returned.
        If `decide` returns None, nothing is written and this returns None
        (not eligible / lost the race to a concurrent claimant).

        Writing payload and status together in one statement is deliberate:
        an earlier version of this method updated only the status column,
        leaving a window where a concurrent claimant's eligibility check --
        which reads status from the (now stale) payload, not the column --
        could still see the pre-claim state and wrongly claim the row too.

        Relies on BEGIN IMMEDIATE (AsyncSQLiteDatabase.run(write=True))
        serializing concurrent callers at the whole-database level -- a
        second caller's transaction genuinely blocks until this one commits,
        then re-reads the already-updated row and correctly finds it no
        longer eligible via `decide`."""
        if self.table not in STATUS_TABLES:
            raise ValueError(f"claim_if_processable requires a status column: {self.table}")

        def op(connection: sqlite3.Connection) -> Any | None:
            row = connection.execute(f"SELECT * FROM {self.table} WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                return None
            current = hydrate(self.table, decode_payload(row["payload"]))
            new_value = decide(current)
            if new_value is None:
                return None
            now = utc_now()
            connection.execute(
                f"UPDATE {self.table} SET payload=?, status=?, updated_at=? WHERE record_id=?",
                (encode_payload(new_value), new_value.status.value, now, record_id),
            )
            return new_value

        return await self.database.run(op, write=True)

    async def list_all(self, limit: int = 1000) -> list[Mapping[str, Any]]:
        if limit < 1 or limit > 10000:
            raise ValueError("limit must be between 1 and 10000")

        def select(connection: sqlite3.Connection) -> list[Mapping[str, Any]]:
            rows = connection.execute(
                f"SELECT * FROM {self.table} ORDER BY created_at ASC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = decode_payload(item["payload"])
                result.append(item)
            return result

        return await self.database.run(select)

    async def list_by_status(self, status: str, limit: int = 100) -> list[Mapping[str, Any]]:
        if self.table not in STATUS_TABLES:
            raise ValueError(f"{self.table} has no status")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        def select(connection: sqlite3.Connection) -> list[Mapping[str, Any]]:
            rows = connection.execute(
                f"SELECT * FROM {self.table} WHERE status=? ORDER BY created_at ASC LIMIT ?",
                (status, limit),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = decode_payload(item["payload"])
                result.append(item)
            return result

        return await self.database.run(select)


@dataclass(frozen=True, slots=True)
class PersistenceRepositories:
    opportunities: RecordRepository
    trade_intents: RecordRepository
    orders: RecordRepository
    fills: RecordRepository
    settlements: RecordRepository
    holdings: RecordRepository
    position_lots: RecordRepository
    cash_ledger: RecordRepository
    pnl_records: RecordRepository
    reconciliation_records: RecordRepository
    trading_sessions: RecordRepository
    audit_events: RecordRepository
    scan_runs: RecordRepository
    equity_snapshots: RecordRepository

    @classmethod
    def create(cls, database: AsyncSQLiteDatabase) -> PersistenceRepositories:
        return cls(**{name: RecordRepository(database, name) for name in cls.__dataclass_fields__})
