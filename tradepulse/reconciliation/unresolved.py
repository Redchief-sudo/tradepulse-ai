"""The durable fill cursor C and unresolved broker executions (Rev.140, spec §3.1a).

Invariant K1: every FILL activity at or before C has either an exact-ID
accounting match (a local Fill or a quarantine execution) or an
``unresolved_broker_executions`` row. C advances only in the transaction
that records every unaccounted FILL it passes, so a crash leaves both or
neither. An unresolved execution stays open, at any age, until exact
evidence writes its outcome; no command, flag or timeout does.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from tradepulse.broker.port import BrokerActivity
from tradepulse.broker.symbols import broker_symbol_asset_key
from tradepulse.models.quarantine import UnresolvedBrokerExecution, UnresolvedExecutionOutcome
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.codec import decode_payload, encode_payload

FILL_CURSOR_RECORD_ID = "fill_reconciliation_cursor"


@dataclass(frozen=True, slots=True)
class PassResult:
    cursor: str | None  # C after the pass; None when the page was not proven
    recorded: tuple[str, ...]  # activity IDs newly recorded as unresolved


def raw_digest(activity: BrokerActivity) -> str:
    return sha256(encode_payload(dict(activity.raw)).encode()).hexdigest()


def _accounted(connection: sqlite3.Connection, activity_id: str) -> bool:
    """An exact-ID match only: never the old time-window heuristic."""
    return bool(
        connection.execute("SELECT 1 FROM fills WHERE record_id=? OR broker_fill_id=?", (activity_id, activity_id)).fetchone()
        or connection.execute("SELECT 1 FROM quarantine_executions WHERE record_id=?", (activity_id,)).fetchone())


def _cursor(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT payload FROM broker_activity_cursors WHERE record_id=?",
                             (FILL_CURSOR_RECORD_ID,)).fetchone()
    return None if row is None else decode_payload(row[0])["cursor"]


def _advance_cursor(connection: sqlite3.Connection, cursor: str, pass_id: str, now: datetime) -> None:
    previous = _cursor(connection)
    if previous is not None and cursor < previous:
        raise ValueError(f"FILL_CURSOR_REGRESSION: {cursor} before {previous}")
    payload = encode_payload({"cursor": cursor, "pass_id": pass_id, "advanced_at": now})
    connection.execute(
        "INSERT INTO broker_activity_cursors (record_id,status,payload,created_at,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (FILL_CURSOR_RECORD_ID, "active", payload, now.isoformat(), now.isoformat()))


async def record_pass(repositories: PersistenceRepositories, activities: Sequence[BrokerActivity], *,
                      page_proven: bool, pass_id: str, now: datetime) -> PassResult:
    """Record every unaccounted FILL in ``activities`` (those after C, in
    page-token order) and advance C to the last of them, in one transaction.
    An unproven page records nothing and leaves C where it was."""
    if not page_proven:
        return PassResult(None, ())

    def write(connection: sqlite3.Connection) -> PassResult:
        recorded = []
        for activity in activities:
            if activity.activity_type != "FILL" or _accounted(connection, activity.activity_id):
                continue
            if activity.side is None or activity.qty is None or activity.price is None or activity.transaction_time is None:
                raise ValueError(f"INVENTORY_EVIDENCE_INVALID: unparsable FILL {activity.activity_id}")
            record = UnresolvedBrokerExecution(
                activity_id=activity.activity_id,
                asset_key=broker_symbol_asset_key(activity.symbol),
                side=activity.side, quantity=activity.qty, price=activity.price,
                transaction_time=activity.transaction_time, raw_sha256=raw_digest(activity),
                first_seen_at=now, pass_id=pass_id)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO unresolved_broker_executions (record_id,payload,created_at) VALUES (?,?,?)",
                (record.activity_id, encode_payload(record), now.isoformat())).rowcount
            if inserted:
                recorded.append(record.activity_id)
        if activities:
            _advance_cursor(connection, activities[-1].activity_id, pass_id, now)
        return PassResult(_cursor(connection), tuple(recorded))

    return await repositories.unresolved_broker_executions.database.run(write, write=True)


async def fill_cursor(repositories: PersistenceRepositories) -> str | None:
    return await repositories.broker_activity_cursors.database.run(_cursor)


async def open_unresolved(repositories: PersistenceRepositories, asset_key: str | None = None) -> list[UnresolvedBrokerExecution]:
    outcomes = {row["record_id"] for row in await paginate_all_rows(repositories.unresolved_execution_outcomes)}
    rows = [hydrate("unresolved_broker_executions", row["payload"])
            for row in await paginate_all_rows(repositories.unresolved_broker_executions)]
    return [row for row in rows if row.activity_id not in outcomes and (asset_key is None or row.asset_key == asset_key)]


async def write_outcome_if_proven(repositories: PersistenceRepositories, activity: BrokerActivity, *, now: datetime) -> bool:
    """Write the outcome only from exact evidence: the activity's raw payload
    unchanged since it was recorded, an exact-ID local Fill (or quarantine
    execution) with identical side, quantity, price and time, and for a Fill
    exactly one ``fill:cash:<activity id>`` entry. Returns whether it wrote."""
    identifier = activity.activity_id

    def write(connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT payload FROM unresolved_broker_executions WHERE record_id=?", (identifier,)).fetchone()
        if row is None or connection.execute("SELECT 1 FROM unresolved_execution_outcomes WHERE record_id=?",
                                             (identifier,)).fetchone():
            return False
        recorded = hydrate("unresolved_broker_executions", decode_payload(row[0]))
        if raw_digest(activity) != recorded.raw_sha256:
            return False
        facts = (recorded.side, recorded.quantity, recorded.price, recorded.transaction_time)
        fill_row = connection.execute("SELECT payload FROM fills WHERE record_id=? OR broker_fill_id=?",
                                      (identifier, identifier)).fetchone()
        evidence: dict[str, object] = {"raw_sha256": recorded.raw_sha256}
        outcome: Literal["matched_fill", "quarantine_execution"]
        if fill_row is not None:
            fill = hydrate("fills", decode_payload(fill_row[0]))
            if (fill.side, fill.quantity, fill.price, fill.filled_at) != facts:
                return False
            cash = connection.execute("SELECT payload FROM cash_ledger WHERE idempotency_key=?",
                                      ("fill:cash:" + fill.fill_id,)).fetchall()
            if len(cash) != 1:
                return False
            outcome, matched = "matched_fill", fill.fill_id
            evidence.update(fill=fill_row[0], cash=cash[0][0])
        else:
            execution_row = connection.execute("SELECT payload FROM quarantine_executions WHERE record_id=?",
                                               (identifier,)).fetchone()
            if execution_row is None:
                return False
            execution = hydrate("quarantine_executions", decode_payload(execution_row[0]))
            if (execution.side, execution.quantity, execution.price, execution.executed_at) != facts \
                    or execution.raw_sha256 != recorded.raw_sha256:
                return False
            from tradepulse.quarantine.resolve import cash_issue

            if cash_issue(connection, execution) is not None:  # I1: exactly one equal cash entry
                return False
            outcome, matched = "quarantine_execution", execution.activity_id
            evidence.update(execution=execution_row[0])
        result = UnresolvedExecutionOutcome(
            activity_id=identifier, outcome=outcome, matched_record_id=matched,
            evidence_sha256=sha256(encode_payload(evidence).encode()).hexdigest(), resolved_at=now)
        connection.execute("INSERT INTO unresolved_execution_outcomes (record_id,payload,created_at) VALUES (?,?,?)",
                           (identifier, encode_payload(result), now.isoformat()))
        return True

    return await repositories.unresolved_execution_outcomes.database.run(write, write=True)


async def relatch_if_open_unresolved(repositories: PersistenceRepositories, now: datetime) -> bool:
    """I8 extended: an open unresolved execution latches the integrity block,
    even when the process that recorded it lost its latch call."""
    from tradepulse.risk import latch_financial_integrity_block

    open_rows = await open_unresolved(repositories)
    if not open_rows:
        return False
    ids = ", ".join(sorted(row.activity_id for row in open_rows))
    await latch_financial_integrity_block(repositories, f"UNRESOLVED_BROKER_EXECUTION: {ids}", clock=lambda: now)
    return True
