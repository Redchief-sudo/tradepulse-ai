from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .codec import decode_payload, encode_payload
from .database import AsyncSQLiteDatabase, RepositoryPaginationError
from .hydration import hydrate

_JSON_FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_json_field_name(field: str) -> None:
    """Guards max_by_json_field/list_by_json_time_range's dynamic JSON
    path -- the path itself is always passed as a bound parameter (never
    string-concatenated into SQL text, so this is not a SQL-injection
    fix), but an unvalidated field name would silently produce a
    meaningless sort key or an always-empty match instead of a clear
    error."""
    if not _JSON_FIELD_NAME_RE.match(field):
        raise ValueError(f"invalid JSON field name: {field!r}")


TABLES = {
    "opportunities", "trade_intents", "orders", "fills", "settlements", "holdings",
    "position_lots", "cash_ledger", "pnl_records", "reconciliation_records",
    "trading_sessions", "audit_events", "scan_runs", "equity_snapshots", "ai_responses",
    "trade_attributions", "rejected_candidates",
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

    async def mutate(self, record_id: str, decide: Callable[[Any], Any | None]) -> Any | None:
        """Atomic read-hydrate-decide-write for holdings/position_lots -- the
        non-status sibling of claim_if_processable, needed once a table has
        more than one concurrent writer (Outcome Attribution: both
        settlement's _project_lot and the position monitor now write
        position_lots, running on independent concurrent asyncio lanes --
        see cli.py::_run_trading_supervisor). A plain get()-then-update()
        pair spans two separate database.run() calls and is NOT atomic
        against a concurrent writer in between; this does the read, the
        caller's decision, and the write inside ONE BEGIN IMMEDIATE
        transaction, exactly like claim_if_processable already does for
        STATUS_TABLES. `decide` returns a new value to persist, or None to
        leave the row unchanged (not found, or the caller determines no
        update is needed -- e.g. the observed extremum didn't move, or the
        row's state no longer qualifies by the time the transaction runs)."""
        if self.table not in {"holdings", "position_lots"}:
            raise ValueError(f"mutate requires an updatable current-state table: {self.table}")

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
                f"UPDATE {self.table} SET payload=?, updated_at=? WHERE record_id=?",
                (encode_payload(new_value), now, record_id),
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

    async def list_recent(self, limit: int = 50) -> list[Mapping[str, Any]]:
        """Newest-first, unlike list_all (oldest-first) -- for "recent
        activity" reads (the dashboard's primary read pattern). Uses the
        exact same `created_at` column list_all/list_by_status already sort
        by, just descending -- verified present identically on every table
        in TABLES (see persistence/database.py's schema), never a
        caller-supplied column name."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        def select(connection: sqlite3.Connection) -> list[Mapping[str, Any]]:
            rows = connection.execute(
                f"SELECT * FROM {self.table} ORDER BY created_at DESC LIMIT ?", (limit,)
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

    async def list_by_statuses(
        self, statuses: Sequence[str], limit: int = 500, *, after: tuple[str, str] | None = None,
    ) -> list[Mapping[str, Any]]:
        """WHERE status IN (...), keyset-paginated via (created_at,
        record_id) -- a compound cursor, not a bare created_at comparison,
        since two rows can share a microsecond-precision timestamp under
        real write volume and a bare cursor could then skip or repeat a
        row. Filters at the SQL level (never a fetch-then-filter), so a
        row in a matching status is never excluded by how much history in
        OTHER statuses exists -- see list_all_by_statuses below for the
        no-ceiling caller-facing helper built on this."""
        if self.table not in STATUS_TABLES:
            raise ValueError(f"{self.table} has no status")
        if not statuses:
            raise ValueError("statuses must not be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        placeholders = ",".join("?" for _ in statuses)
        where = f"status IN ({placeholders})"
        params: list[Any] = list(statuses)
        if after is not None:
            where += " AND (created_at, record_id) > (?, ?)"
            params.extend(after)
        params.append(limit)

        def select(connection: sqlite3.Connection) -> list[Mapping[str, Any]]:
            rows = connection.execute(
                f"SELECT * FROM {self.table} WHERE {where} ORDER BY created_at ASC, record_id ASC LIMIT ?", params,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = decode_payload(item["payload"])
                result.append(item)
            return result

        return await self.database.run(select)

    async def list_by_json_time_range(
        self, field: str, start: datetime, end: datetime, limit: int = 500, *, after: tuple[str, str] | None = None,
    ) -> list[Mapping[str, Any]]:
        """WHERE <field> (a validated top-level JSON key) is an ISO8601
        datetime string in [start, end) -- string comparison is safe
        because every persisted aware datetime is normalized to UTC before
        serialization (models/base.py::require_aware), so every stored
        value shares the same +00:00 offset and sorts lexicographically =
        chronologically. Keyset-paginated via (created_at, record_id),
        same contract as list_by_statuses -- no assumption about how many
        rows can fall inside one window (a single economic trade can
        produce many partial-fill rows)."""
        _validate_json_field_name(field)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")

        where = f"json_extract(payload, '$.{field}') >= ? AND json_extract(payload, '$.{field}') < ?"
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if after is not None:
            where += " AND (created_at, record_id) > (?, ?)"
            params.extend(after)
        params.append(limit)

        def select(connection: sqlite3.Connection) -> list[Mapping[str, Any]]:
            rows = connection.execute(
                f"SELECT * FROM {self.table} WHERE {where} ORDER BY created_at ASC, record_id ASC LIMIT ?", params,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = decode_payload(item["payload"])
                result.append(item)
            return result

        return await self.database.run(select)

    async def max_by_json_field(self, field: str) -> Mapping[str, Any] | None:
        """Returns the row whose payload has the largest value for the
        given top-level JSON field (compared numerically via a REAL cast
        used ONLY as the sort key -- the returned row's payload is the
        original decoded JSON, so the caller reads back the exact Decimal
        string, never a lossy float), or None if the table is empty. Used
        where a caller needs a genuine all-time extremum (e.g. drawdown's
        peak-equity search) that a row-capped list cannot correctly answer
        once the table outgrows any fixed limit -- this does one full-table
        SQL aggregation instead."""
        _validate_json_field_name(field)

        def select(connection: sqlite3.Connection) -> Mapping[str, Any] | None:
            row = connection.execute(
                f"SELECT * FROM {self.table} ORDER BY CAST(json_extract(payload, '$.{field}') AS REAL) DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = decode_payload(result["payload"])
            return result

        return await self.database.run(select)

    async def exists_with_status_and_asset(self, statuses: Sequence[str], asset: Any) -> bool:
        """trade_intents-only SQL EXISTS, matching asset_identity_key's own
        composition (models/market.py: f"{asset_class}:{venue-or-default}:
        {native_asset_id}") via json_extract against the payload --
        correct regardless of table size, never a fetch-N-rows-then-filter
        blind spot. Kept table-specific (raises on any other table) rather
        than a generic "match any JSON subfield" primitive, matching
        mutate()/claim_if_processable()'s existing table-gating precedent
        -- this replicates one specific, load-bearing identity definition,
        not an open-ended query surface."""
        if self.table != "trade_intents":
            raise ValueError("exists_with_status_and_asset is only defined for trade_intents")
        if not statuses:
            raise ValueError("statuses must not be empty")

        placeholders = ",".join("?" for _ in statuses)
        venue = (asset.venue or "default").lower()
        params: list[Any] = [*statuses, asset.asset_class.value, venue, asset.native_asset_id]

        def select(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                f"""SELECT 1 FROM {self.table}
                WHERE status IN ({placeholders})
                  AND json_extract(payload, '$.asset.asset_class') = ?
                  AND LOWER(COALESCE(json_extract(payload, '$.asset.venue'), 'default')) = ?
                  AND json_extract(payload, '$.asset.native_asset_id') = ?
                LIMIT 1""",
                params,
            ).fetchone()
            return row is not None

        return await self.database.run(select)


async def _paginate_all(
    fetch_page: Callable[[tuple[str, str] | None, int], Awaitable[list[Mapping[str, Any]]]],
    *, batch_size: int = 500,
) -> list[Mapping[str, Any]]:
    """Pages until a batch comes back shorter than batch_size (true
    exhaustion -- not a guess). No max-batches ceiling: a valid unresolved
    set of any size is fully returned. The only way this returns early is
    a short final page; the only way it raises is a stalled/repeating
    cursor, which means SOMETHING IS WRONG (a bug, or two rows colliding
    on (created_at, record_id)) and must fail loudly rather than silently
    hand back partial financial state."""
    results: list[Mapping[str, Any]] = []
    after: tuple[str, str] | None = None
    while True:
        batch = await fetch_page(after, batch_size)
        if not batch:
            break
        results.extend(batch)
        new_after = (batch[-1]["created_at"], batch[-1]["record_id"])
        if after is not None and new_after <= after:
            raise RepositoryPaginationError(f"pagination cursor failed to advance (stalled at {new_after})")
        after = new_after
        if len(batch) < batch_size:
            break
    return results


async def list_all_by_statuses(
    repo: RecordRepository, statuses: Sequence[str], *, batch_size: int = 500,
) -> list[Mapping[str, Any]]:
    """No-ceiling caller-facing wrapper around list_by_statuses -- pages
    through the FULL matching set regardless of how large it is."""
    return await _paginate_all(lambda after, limit: repo.list_by_statuses(statuses, limit=limit, after=after), batch_size=batch_size)


async def list_all_by_json_time_range(
    repo: RecordRepository, field: str, start: datetime, end: datetime, *, batch_size: int = 500,
) -> list[Mapping[str, Any]]:
    """No-ceiling caller-facing wrapper around list_by_json_time_range."""
    return await _paginate_all(
        lambda after, limit: repo.list_by_json_time_range(field, start, end, limit=limit, after=after), batch_size=batch_size,
    )


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
    ai_responses: RecordRepository
    trade_attributions: RecordRepository
    rejected_candidates: RecordRepository

    @classmethod
    def create(cls, database: AsyncSQLiteDatabase) -> PersistenceRepositories:
        return cls(**{name: RecordRepository(database, name) for name in cls.__dataclass_fields__})
