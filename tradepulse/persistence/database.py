from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

T = TypeVar("T")


class DatabaseError(RuntimeError):
    """Persistence failure that callers must classify explicitly."""


class RepositoryPaginationError(DatabaseError):
    """A keyset pagination cursor failed to strictly advance between pages
    -- a bug or a (created_at, record_id) collision, either way a signal
    that the result set may be incomplete. Raised rather than silently
    returning a partial page: financial-critical callers (settlement,
    pending exposure, daily trade/PnL accounting) must never mistake a
    truncated result for the complete unresolved/in-window set."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_identity (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  database_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
  ever_evidence INTEGER NOT NULL CHECK(ever_evidence IN (0,1)),
  legacy_database INTEGER NOT NULL CHECK(legacy_database IN (0,1)),
  generation_id TEXT, checkpoint_id TEXT, manifest_digest TEXT
);
CREATE TABLE IF NOT EXISTS accounting_epochs (
  record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_activity_inbox (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_activity_cursors (
  record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_intents (
  record_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  record_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
  record_id TEXT PRIMARY KEY, broker_fill_id TEXT UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
  record_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_lots (
  record_id TEXT PRIMARY KEY, originating_fill_id TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_ledger (
  record_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl_records (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_records (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_sessions (
  record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_responses (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_attributions (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejected_candidates (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locks (
  lock_key TEXT PRIMARY KEY, owner_token TEXT NOT NULL,
  acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, command TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS integrity_holds (
  record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_status ON settlements(status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_trade_intents_status ON trade_intents(status, created_at);
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_created ON equity_snapshots(created_at);

-- Rev.94/95 performance hardening: closes the full-scan-plus-sort plans a
-- read-only EXPLAIN QUERY PLAN audit found on every JSON-identity/whole-
-- table-pagination query family below (see docs/persistence-index-
-- performance-audit.md). `CREATE INDEX IF NOT EXISTS` makes this a no-op
-- migration on a database that already has these -- and, since
-- AsyncSQLiteDatabase.initialize() runs this whole script unconditionally
-- on every startup (not just first-time setup), an existing database
-- backfills the missing indexes the next time the app starts, without a
-- separate migration step. Ordered by the audit's priority: proven-HIGH
-- findings on financial-authority/genuinely-growing tables first.
CREATE INDEX IF NOT EXISTS idx_position_lots_asset ON position_lots(
  json_extract(payload,'$.asset.asset_class'),
  LOWER(COALESCE(json_extract(payload,'$.asset.venue'),'default')),
  json_extract(payload,'$.asset.native_asset_id'),
  created_at, record_id
);
CREATE INDEX IF NOT EXISTS idx_position_lots_created ON position_lots(created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_fills_trade_intent_id ON fills(json_extract(payload,'$.trade_intent_id'), created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_settlements_trade_intent_id ON settlements(json_extract(payload,'$.trade_intent_id'), created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_settlements_broker_fill_id ON settlements(json_extract(payload,'$.broker_fill_id'), created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_trade_intents_broker_order_id ON trade_intents(json_extract(payload,'$.broker_order_id'), created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_fills_filled_at ON fills(json_extract(payload,'$.filled_at'), created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_equity_total_equity_real ON equity_snapshots(CAST(json_extract(payload,'$.total_equity') AS REAL));
CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status, created_at, record_id);
CREATE INDEX IF NOT EXISTS idx_integrity_holds_status ON integrity_holds(status, created_at, record_id);
-- Rev.140 quarantine evidence: append-only, enforced by the triggers
-- create_quarantine_triggers writes and verify_schema_triggers checks.
CREATE TABLE IF NOT EXISTS short_exposure_liabilities (
  record_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unmanaged_long_inventory (
  record_id TEXT PRIMARY KEY, cover_activity_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_executions (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_resolutions (
  record_id TEXT PRIMARY KEY, resolution_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_fee_allocations (
  record_id TEXT PRIMARY KEY, allocation_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_fee_recognitions (
  record_id TEXT PRIMARY KEY, recognition_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_lot_conversions (
  record_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unresolved_broker_executions (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unresolved_execution_outcomes (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fill_cursor_bootstrap (
  record_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
-- Rev.144 §3.7: the calendar cross-check lifecycle, append-only. Date and
-- instrument rows exist only for a check with a completed event.
CREATE TABLE IF NOT EXISTS calendar_check_events (
  record_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, event TEXT NOT NULL
    CHECK(event IN ('started','fetched','completed','failed')),
  broker TEXT NOT NULL, venue TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(check_id, event)
);
CREATE TABLE IF NOT EXISTS calendar_check_dates (
  record_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, broker TEXT NOT NULL, venue TEXT NOT NULL, date TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('agreed','contested')), checked_at TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(check_id, broker, venue, date)
);
CREATE INDEX IF NOT EXISTS idx_calendar_check_dates ON calendar_check_dates(broker, venue, date, checked_at);
CREATE TABLE IF NOT EXISTS instrument_hours (
  record_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, broker TEXT NOT NULL, instrument_key TEXT NOT NULL,
  date TEXT NOT NULL, checked_at TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(check_id, broker, instrument_key, date)
);
CREATE INDEX IF NOT EXISTS idx_instrument_hours ON instrument_hours(broker, instrument_key, date, checked_at);
-- Rev.143 §3.10: announcements and attribution rows are immutable; the
-- per-activity status row is the one mutable dividend record.
CREATE TABLE IF NOT EXISTS corporate_action_announcements (
  record_id TEXT PRIMARY KEY, announcement_id TEXT NOT NULL, sha256 TEXT NOT NULL, symbol TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(announcement_id, sha256)
);
CREATE TABLE IF NOT EXISTS dividend_attributions (
  record_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, component TEXT NOT NULL CHECK(component IN ('income','withholding')),
  bucket TEXT NOT NULL CHECK(bucket IN ('strategy','adopted','opening_unadopted','excluded_unallocated')),
  holder_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(activity_id, component, bucket, holder_id)
);
CREATE TABLE IF NOT EXISTS dividend_attribution_status (
  record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

# These triggers record provenance even if an operator later deletes all rows.
# Old files cannot prove that their previously deleted history was empty.
EVIDENCE_TABLES = (
    "accounting_epochs", "broker_activity_inbox", "broker_activity_cursors",
    "opportunities", "trade_intents", "orders", "fills", "settlements", "holdings",
    "position_lots", "cash_ledger", "pnl_records", "reconciliation_records",
    "scan_runs", "equity_snapshots", "trade_attributions", "integrity_holds",
    "short_exposure_liabilities", "unmanaged_long_inventory", "quarantine_executions", "quarantine_resolutions",
    "quarantine_fee_allocations", "quarantine_fee_recognitions", "legacy_lot_conversions",
    "unresolved_broker_executions", "unresolved_execution_outcomes", "fill_cursor_bootstrap",
)

QUARANTINE_TABLES = EVIDENCE_TABLES[-10:]
CALENDAR_TABLES = ("calendar_check_events", "calendar_check_dates", "instrument_hours")
DIVIDEND_TABLES = ("corporate_action_announcements", "dividend_attributions")
APPEND_ONLY_TABLES = QUARANTINE_TABLES + CALENDAR_TABLES + DIVIDEND_TABLES

# Rev.144 §3.7: at most one terminal event per check, and date or hours rows
# only for a check already completed in the same transaction.
CALENDAR_INSERT_TRIGGERS = (
    ("calendar_one_terminal_event",
     "CREATE TRIGGER calendar_one_terminal_event BEFORE INSERT ON calendar_check_events "
     "WHEN NEW.event IN ('completed','failed') AND EXISTS (SELECT 1 FROM calendar_check_events "
     "WHERE check_id = NEW.check_id AND event IN ('completed','failed')) "
     "BEGIN SELECT RAISE(ABORT, 'calendar check already ended'); END"),
    *((f"calendar_rows_need_completed_{table}",
       f"CREATE TRIGGER calendar_rows_need_completed_{table} BEFORE INSERT ON {table} "
       f"WHEN NOT EXISTS (SELECT 1 FROM calendar_check_events WHERE check_id = NEW.check_id AND event = 'completed') "
       f"BEGIN SELECT RAISE(ABORT, '{table} rows need a completed check'); END")
      for table in ("calendar_check_dates", "instrument_hours")),
)


def _quarantine_trigger(table: str, operation: str) -> tuple[str, str]:
    """The one template for both creating and verifying a trigger. Written on
    one line so SQLite stores it in sqlite_master exactly as given."""
    name = f"quarantine_no_{operation.lower()}_{table}"
    return name, (f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                  f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")


def create_quarantine_triggers(connection: sqlite3.Connection, tables: Iterable[str]) -> None:
    """Only for tables created in the same initialize: re-creating a missing
    trigger on an existing table would hide exactly what verification exists
    to catch."""
    for table in tables:
        for operation in ("UPDATE", "DELETE"):
            connection.execute(_quarantine_trigger(table, operation)[1])


def create_calendar_insert_triggers(connection: sqlite3.Connection, tables: Iterable[str]) -> None:
    created = set(tables)
    for name, sql in CALENDAR_INSERT_TRIGGERS:
        if any(f" ON {table} " in sql for table in created):
            connection.execute(sql)


def verify_schema_triggers(connection: sqlite3.Connection) -> None:
    """Every quarantine table refuses UPDATE and DELETE (spec §3.11, I9).
    Raises SCHEMA_TRIGGER_MISSING when a trigger is absent or its SQL differs."""
    expected_triggers = [_quarantine_trigger(table, operation) for table in APPEND_ONLY_TABLES
                         for operation in ("UPDATE", "DELETE")] + list(CALENDAR_INSERT_TRIGGERS)
    for name, expected in expected_triggers:
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or row[0] != expected:
            raise DatabaseError(f"SCHEMA_TRIGGER_MISSING:{name}")


def initialize_identity(connection: sqlite3.Connection, *, legacy: bool) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO verification_identity "
        "(singleton,database_id,created_at,ever_evidence,legacy_database) VALUES (1,?,?,?,?)",
        (str(uuid4()), datetime.now(UTC).isoformat(), int(legacy), int(legacy)),
    )
    for table in EVIDENCE_TABLES:
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS evidence_history_{table}
            AFTER INSERT ON {table} BEGIN
              UPDATE verification_identity SET ever_evidence=1 WHERE singleton=1;
            END""")
    connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS verification_identity_no_delete
        BEFORE DELETE ON verification_identity BEGIN
          SELECT RAISE(ABORT, 'database identity cannot be deleted');
        END;
        CREATE TRIGGER IF NOT EXISTS verification_identity_immutable
        BEFORE UPDATE ON verification_identity
        WHEN NEW.database_id != OLD.database_id OR NEW.created_at != OLD.created_at
          OR NEW.singleton != OLD.singleton OR NEW.legacy_database != OLD.legacy_database
          OR NEW.ever_evidence < OLD.ever_evidence
          OR (OLD.generation_id IS NOT NULL AND
              (NEW.generation_id IS NOT OLD.generation_id
               OR NEW.checkpoint_id IS NOT OLD.checkpoint_id
               OR NEW.manifest_digest IS NOT OLD.manifest_digest))
        BEGIN SELECT RAISE(ABORT, 'database identity cannot be reset or rebound'); END;
    """)


class AsyncSQLiteDatabase:
    """SQLite boundary that moves every blocking operation off the event loop."""

    def __init__(self, database_url: str) -> None:
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise DatabaseError("Phase 2 supports only sqlite:/// database URLs")
        raw_path = database_url[len(prefix):]
        if not raw_path:
            raise DatabaseError("SQLite path is required")
        self.path = Path(raw_path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    async def initialize(self) -> None:
        def initialize_sync() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists() and self.path.stat().st_size > 0
            # A sqlite3 connection's own context manager commits or rolls
            # back but never closes; left open, it is closed whenever the
            # garbage collector reaches it, checkpointing the WAL into the
            # database file at an unpredictable moment.
            connection = self._connect()
            try:
                with connection:
                    present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    connection.executescript(SCHEMA)
                    initialize_identity(connection, legacy=existed)
                    created = [t for t in APPEND_ONLY_TABLES if t not in present]
                    create_quarantine_triggers(connection, created)
                    create_calendar_insert_triggers(connection, created)
                verify_schema_triggers(connection)
            finally:
                connection.close()

        await asyncio.to_thread(initialize_sync)

    async def run(self, operation: Callable[[sqlite3.Connection], T], *, write: bool = False) -> T:
        def execute_sync() -> T:
            connection = self._connect()
            try:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                if write:
                    connection.commit()
                return result
            except Exception:
                if write:
                    connection.rollback()
                raise
            finally:
                connection.close()

        try:
            return await asyncio.to_thread(execute_sync)
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
