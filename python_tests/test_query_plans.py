"""EXPLAIN QUERY PLAN regression tests for the Rev.94/95 indexes (see
docs/persistence-index-performance-audit.md). SQLite expression indexes are
matched by exact expression text -- a future refactor that reorders
`LOWER(COALESCE(...))`, changes a JSON path, or alters `max_by_json_field`'s
`CAST` target type silently falls back to a full scan, with no exception
(the audit's own §4/§8 finding). A plan-shape assertion is the only thing
that would catch that drift before it reaches production traffic.

Each test captures the LITERAL SQL text `repositories.py` executes -- via
`sqlite3.Connection.set_trace_callback` on the connection the repository
call itself opens, never a hand-retyped copy of the query -- and runs
EXPLAIN QUERY PLAN against that same database, built through the real
`AsyncSQLiteDatabase.initialize()` application schema path (never scratch
SQL)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    IntegrityHold,
    IntegrityHoldType,
    PortfolioSnapshot,
    PositionLot,
    ScanRun,
    ScanRunStatus,
    ScanTrigger,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
ROWS = 60  # small on purpose: plan SHAPE is row-count-independent (proven at 1k/10k/100k in the audit); this only needs enough rows for ANALYZE to produce non-degenerate stats.


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _analyze(repositories: PersistenceRepositories) -> None:
    def run(connection):
        connection.execute("ANALYZE")
    await repositories.holdings.database.run(run, write=True)


def _asset(i: int) -> AssetIdentity:
    return AssetIdentity(f"SYM{i % 5}", AssetClass.EQUITY, f"alpaca:SYM{i % 5}")


async def _capture_plan(
    repositories: PersistenceRepositories, call: Callable[[], Awaitable[object]],
) -> tuple[str, list[str]]:
    """Runs `call` (one repository read) against its own real connection,
    capturing the literal SELECT text it executes, then runs
    EXPLAIN QUERY PLAN for that exact text on a fresh connection to the
    same database file. Returns (captured_sql, plan_detail_lines)."""
    database = repositories.holdings.database
    statements: list[str] = []
    original_connect = database._connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    database._connect = traced_connect
    try:
        await call()
    finally:
        database._connect = original_connect

    selects = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert selects, f"no SELECT captured (saw: {statements!r})"
    sql = selects[-1]

    connection = original_connect()
    try:
        plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    finally:
        connection.close()
    return sql, [row["detail"] for row in plan]


async def test_position_lots_list_by_asset_uses_index_and_serves_the_sort(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        lot = PositionLot(
            lot_id=f"lot-{i}", originating_fill_id=f"fill-{i}", asset=_asset(i),
            position_side="long", opened_quantity=Decimal("10"), remaining_quantity=Decimal("10"),
            acquisition_price=Decimal("150"), opened_at=NOW + timedelta(seconds=i),
        )
        await repositories.position_lots.create_once(f"lot-{i}", lot, unique_value=lot.originating_fill_id)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.position_lots.list_by_asset(_asset(0), limit=10),
    )

    assert any("USING INDEX idx_position_lots_asset" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_position_lots_list_page_uses_created_index_with_no_sort(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        lot = PositionLot(
            lot_id=f"lot-{i}", originating_fill_id=f"fill-{i}", asset=_asset(i),
            position_side="long", opened_quantity=Decimal("10"), remaining_quantity=Decimal("10"),
            acquisition_price=Decimal("150"), opened_at=NOW + timedelta(seconds=i),
        )
        await repositories.position_lots.create_once(f"lot-{i}", lot, unique_value=lot.originating_fill_id)
    await _analyze(repositories)

    sql, detail = await _capture_plan(repositories, lambda: repositories.position_lots.list_page(limit=10))

    assert any("USING INDEX idx_position_lots_created" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


def _fill(i: int) -> Fill:
    return Fill(
        fill_id=f"fill-{i}", trade_intent_id=f"ti-{i}", order_id=f"order-{i}", asset=_asset(i),
        side=Side.BUY, execution_mode=ExecutionMode.PAPER, quantity=Decimal("1"), price=Decimal("100"),
        fees=Decimal("0"), slippage=Decimal("0"), filled_at=NOW + timedelta(seconds=i),
        broker_fill_id=f"broker-fill-{i}",
    )


async def test_fills_list_by_json_field_trade_intent_id_uses_index_and_serves_the_sort(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        fill = _fill(i)
        await repositories.fills.create_once(f"fill-{i}", fill, unique_value=fill.broker_fill_id)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.fills.list_by_json_field("trade_intent_id", "ti-0", limit=10),
    )

    assert any("USING INDEX idx_fills_trade_intent_id" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_fills_list_by_json_time_range_filled_at_uses_index_but_keeps_the_residual_sort(tmp_path) -> None:
    """Row #6 of the audit: a RANGE predicate on the leading expression
    column removes the full-table scan but can NOT also satisfy the
    trailing (created_at, record_id) ORDER BY -- the temp b-tree stays.
    This test asserts that qualified claim exactly, so it fails loudly if
    a future SQLite/query change makes the sort disappear (an overclaim
    fixed silently) or the index stops being chosen at all (a regression)."""
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        fill = _fill(i)
        await repositories.fills.create_once(f"fill-{i}", fill, unique_value=fill.broker_fill_id)
    await _analyze(repositories)

    start = NOW
    end = NOW + timedelta(seconds=ROWS)
    sql, detail = await _capture_plan(
        repositories, lambda: repositories.fills.list_by_json_time_range("filled_at", start, end, limit=10),
    )

    assert any("USING INDEX idx_fills_filled_at" in line for line in detail), (sql, detail)
    assert any("TEMP B-TREE" in line for line in detail), (
        "expected the residual sort to still be present per audit row #6 -- if this now "
        f"passes without a temp b-tree, update this test AND the audit's §1/§4 claim: {detail}"
    )


def _settlement(i: int) -> SettlementEvent:
    return SettlementEvent(
        settlement_event_id=f"se-{i}", fill_id=f"fill-{i}", trade_intent_id=f"ti-{i}", asset=_asset(i),
        side=Side.BUY, execution_mode=ExecutionMode.PAPER, quantity=Decimal("1"), price=Decimal("100"),
        occurred_at=NOW + timedelta(seconds=i), status=SettlementStatus.PENDING,
        broker_fill_id=f"broker-fill-{i}",
    )


async def test_settlements_list_by_json_field_trade_intent_id_uses_index(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        settlement = _settlement(i)
        await repositories.settlements.create_once(f"se-{i}", settlement, status=settlement.status.value, unique_value=settlement.fill_id)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.settlements.list_by_json_field("trade_intent_id", "ti-0", limit=10),
    )

    assert any("USING INDEX idx_settlements_trade_intent_id" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_settlements_list_by_json_field_broker_fill_id_uses_index(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        settlement = _settlement(i)
        await repositories.settlements.create_once(f"se-{i}", settlement, status=settlement.status.value, unique_value=settlement.fill_id)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.settlements.list_by_json_field("broker_fill_id", "broker-fill-0", limit=10),
    )

    assert any("USING INDEX idx_settlements_broker_fill_id" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


def _intent(i: int) -> TradeIntent:
    return TradeIntent(
        f"ti-{i}", f"idem-{i}", f"corr-{i}", _asset(i), Side.BUY, ExecutionMode.PAPER, "manual",
        NOW + timedelta(seconds=i), requested_quantity=Decimal("5"), status=TradeIntentStatus.ACCEPTED,
        broker_order_id=f"broker-order-{i}",
    )


async def test_trade_intents_list_by_json_field_broker_order_id_uses_index(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        intent = _intent(i)
        await repositories.trade_intents.create_once(f"ti-{i}", intent, status=intent.status.value, unique_value=intent.idempotency_key)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.trade_intents.list_by_json_field("broker_order_id", "broker-order-0", limit=10),
    )

    assert any("USING INDEX idx_trade_intents_broker_order_id" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_scan_runs_list_by_statuses_uses_index_with_no_sort(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        run = ScanRun(
            scan_run_id=f"scan-{i}", scan_generation="gen-2", trigger=ScanTrigger.SCHEDULED,
            asset_class=AssetClass.EQUITY, status=ScanRunStatus.FAILED if i % 3 == 0 else ScanRunStatus.COMPLETED,
            started_at=NOW + timedelta(seconds=i), lock_owner_token=f"owner-{i}",
            completed_at=NOW + timedelta(seconds=i, minutes=1),
            error="synthetic failure" if i % 3 == 0 else None,
        )
        await repositories.scan_runs.create_once(f"scan-{i}", run, status=run.status.value)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.scan_runs.list_by_statuses([ScanRunStatus.FAILED.value], limit=10),
    )

    assert any("USING INDEX idx_scan_runs_status" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_integrity_holds_list_by_statuses_uses_index(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        hold_type = IntegrityHoldType.FILL_QUANTITY_DISPUTED if i % 4 == 0 else IntegrityHoldType.VERIFICATION_PENDING
        hold = IntegrityHold(
            broker_order_id=f"broker-order-{i}", trade_intent_id=f"ti-{i}", hold_type=hold_type,
            reason="test", created_at=NOW + timedelta(seconds=i),
        )
        await repositories.integrity_holds.create_once(f"broker-order-{i}", hold, status=hold_type.value)
    await _analyze(repositories)

    sql, detail = await _capture_plan(
        repositories,
        lambda: repositories.integrity_holds.list_by_statuses([IntegrityHoldType.FILL_QUANTITY_DISPUTED.value], limit=10),
    )

    assert any("USING INDEX idx_integrity_holds_status" in line for line in detail), (sql, detail)
    assert not any("TEMP B-TREE" in line for line in detail), (sql, detail)


async def test_equity_snapshots_max_by_json_field_uses_expression_index(tmp_path) -> None:
    """The audit's most fragile finding (§4): this expression index is
    matched to `max_by_json_field`'s query only if `CAST(json_extract(...)
    AS REAL)` is byte-for-byte identical between index and query. A future
    refactor to either side would silently fall back to a full scan -- this
    test is the guard."""
    repositories = await _repositories(tmp_path)
    for i in range(ROWS):
        snapshot = PortfolioSnapshot(
            snapshot_id=f"eq-{i}", as_of=NOW + timedelta(seconds=i), total_equity=Decimal(str(100000 + i)),
            cash_balance=Decimal("1000"), holdings_value=Decimal("99000"), sector_exposure={},
            open_positions=1, outstanding_orders=0, trades_today=0, daily_pnl_pct=Decimal("0"), source="broker",
        )
        await repositories.equity_snapshots.create_once(f"eq-{i}", snapshot)
    await _analyze(repositories)

    sql, detail = await _capture_plan(repositories, lambda: repositories.equity_snapshots.max_by_json_field("total_equity"))

    assert any("USING INDEX idx_equity_total_equity_real" in line for line in detail), (sql, detail)


async def test_initialize_backfills_all_ten_indexes_on_an_already_existing_database(tmp_path) -> None:
    """Proves migration, not just fresh-database creation: a database built
    with an OLDER schema (tables present, none of the Rev.94/95 indexes),
    already holding real rows, receives every new index the next time
    `initialize()` runs -- exactly the path a real deployed `tradepulse.db`
    takes on its next startup. Proves all six things a schema migration
    must prove, not just that the index NAMES appear in sqlite_master:
    (1) the database file is not recreated -- proven by the seeded rows
    surviving, since executescript(SCHEMA) contains no DROP/DELETE and
    initialize() opens the SAME file, never a fresh one; (2) existing rows
    are byte-for-byte unchanged; (3)+(4) all ten indexes are installed,
    via sqlite_master; (5) PRAGMA integrity_check stays ok; (6) a real
    production query against THIS SAME upgraded database actually gets
    served by the new index, not just that an index with the right name
    exists unused."""
    import sqlite3

    from tradepulse.persistence.codec import encode_payload

    db_path = tmp_path / "pre_existing.db"
    pre_existing_schema = """
    CREATE TABLE trade_intents (
      record_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE fills (
      record_id TEXT PRIMARY KEY, broker_fill_id TEXT UNIQUE, payload TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE settlements (
      record_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE position_lots (
      record_id TEXT PRIMARY KEY, originating_fill_id TEXT NOT NULL UNIQUE,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE scan_runs (
      record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE equity_snapshots (
      record_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE integrity_holds (
      record_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_settlements_status ON settlements(status, created_at);
    CREATE INDEX idx_trade_intents_status ON trade_intents(status, created_at);
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(pre_existing_schema)
        # Real rows, real payload shape (via the app's own model classes +
        # encode_payload, never hand-typed JSON) -- an upgrade test that
        # migrates an empty database proves nothing about existing data
        # surviving.
        seeded_lot = PositionLot(
            lot_id="lot-seed", originating_fill_id="fill-seed", asset=_asset(0),
            position_side="long", opened_quantity=Decimal("10"), remaining_quantity=Decimal("10"),
            acquisition_price=Decimal("150"), opened_at=NOW,
        )
        connection.execute(
            "INSERT INTO position_lots (record_id, originating_fill_id, payload, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("lot-seed", "fill-seed", encode_payload(seeded_lot), NOW.isoformat(), NOW.isoformat()),
        )
        seeded_fill = _fill(0)
        connection.execute(
            "INSERT INTO fills (record_id, broker_fill_id, payload, created_at) VALUES (?,?,?,?)",
            (seeded_fill.fill_id, seeded_fill.broker_fill_id, encode_payload(seeded_fill), NOW.isoformat()),
        )
        seeded_intent = TradeIntent(
            "ti-seed", "idem-seed", "corr-seed", _asset(0), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
            requested_quantity=Decimal("10"), broker_order_id="order-seed",
        )
        connection.execute(
            "INSERT INTO trade_intents (record_id, idempotency_key, status, payload, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("ti-seed", "idem-seed", seeded_intent.status.value, encode_payload(seeded_intent), NOW.isoformat(), NOW.isoformat()),
        )
        connection.commit()
        rows_before = {
            "position_lots": connection.execute("SELECT record_id, payload FROM position_lots").fetchall(),
            "fills": connection.execute("SELECT record_id, payload FROM fills").fetchall(),
            "trade_intents": connection.execute("SELECT record_id, payload FROM trade_intents").fetchall(),
        }
    finally:
        connection.close()

    expected_indexes = {
        "idx_position_lots_asset", "idx_position_lots_created", "idx_fills_trade_intent_id",
        "idx_settlements_trade_intent_id", "idx_settlements_broker_fill_id",
        "idx_trade_intents_broker_order_id", "idx_fills_filled_at", "idx_equity_total_equity_real",
        "idx_scan_runs_status", "idx_integrity_holds_status",
    }

    before = sqlite3.connect(db_path)
    try:
        existing_before = {row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        before.close()
    assert not (expected_indexes & existing_before), "test setup bug: pre-existing DB already has a Rev.94/95 index"

    database = AsyncSQLiteDatabase(f"sqlite:///{db_path}")
    await database.initialize()

    after = sqlite3.connect(db_path)
    try:
        existing_after = {row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        integrity = after.execute("PRAGMA integrity_check").fetchone()[0]
        rows_after = {
            "position_lots": after.execute("SELECT record_id, payload FROM position_lots").fetchall(),
            "fills": after.execute("SELECT record_id, payload FROM fills").fetchall(),
            "trade_intents": after.execute("SELECT record_id, payload FROM trade_intents").fetchall(),
        }
    finally:
        after.close()

    assert expected_indexes <= existing_after, expected_indexes - existing_after
    assert integrity == "ok"
    # (2) existing rows survive byte-for-byte -- if initialize() had ever
    # recreated the file instead of migrating it in place, this seeded data
    # would be gone.
    assert rows_after == rows_before

    # (6) a real production query against THIS upgraded database -- not a
    # freshly-created one -- actually gets served by the new index.
    repositories = PersistenceRepositories.create(database)
    sql, detail = await _capture_plan(
        repositories, lambda: repositories.position_lots.list_by_asset(_asset(0), limit=10),
    )
    assert any("USING INDEX idx_position_lots_asset" in line for line in detail), (sql, detail)

    sql, detail = await _capture_plan(
        repositories, lambda: repositories.fills.list_by_json_field("trade_intent_id", "ti-0", limit=10),
    )
    assert any("USING INDEX idx_fills_trade_intent_id" in line for line in detail), (sql, detail)
