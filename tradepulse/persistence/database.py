from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


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
"""


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
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def initialize_sync() -> None:
            with self._connect() as connection:
                connection.executescript(SCHEMA)

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
