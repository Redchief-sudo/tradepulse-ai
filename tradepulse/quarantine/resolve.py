"""`tradepulse quarantine resolve` (Rev.140, spec §3.7, §3.8).

The only writer of a quarantine execution, its cash, its resolutions and
recognitions. Every check of §3.7 step 1 runs before anything is written;
a refusal writes nothing. The writes are one SQLite transaction, which
re-reads the open records itself, so a crash or a concurrent command
leaves everything or nothing (I11: the cash entry shares the fill
namespace ``fill:cash:<activity id>``).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from tradepulse.broker.port import BrokerActivity, BrokerPort
from tradepulse.broker.symbols import broker_symbol_asset_key
from tradepulse.models import (
    AssetIdentity,
    AuditEvent,
    CashLedgerEntry,
    Side,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.models.quarantine import (
    QuarantineExecution,
    QuarantineFeeRecognition,
    QuarantineResolution,
    UnmanagedLongInventory,
    UnresolvedExecutionOutcome,
)
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.runtime_lease import AccountLease

from .corporate_actions import corporate_action_blocks
from .fees import recognize_at_resolution

CASH_NOTE = "quarantine execution cash; accounting-v1"


class ResolveRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ResolveResult:
    resolved: tuple[QuarantineResolution, ...]
    surplus: UnmanagedLongInventory | None


@dataclass(frozen=True, slots=True)
class _Open:
    record_id: str
    sign: int
    quantity: Decimal  # open quantity
    reference_price: Decimal
    multiplier: Decimal
    opened_at: datetime
    asset: AssetIdentity


def raw_digest(activity: BrokerActivity) -> str:
    return sha256(encode_payload(dict(activity.raw)).encode()).hexdigest()


def execution_cash(execution: QuarantineExecution) -> CashLedgerEntry:
    """Derived exactly like ``cash_evidence``: a sale credits, a cover debits."""
    identifier = "fill:cash:" + execution.activity_id
    notional = execution.quantity * execution.price * contract_multiplier_of(execution.asset)
    return CashLedgerEntry(identifier, identifier, notional if execution.side == Side.SELL else -notional,
                           "USD", execution.executed_at, CASH_NOTE)


def _payloads(connection: sqlite3.Connection, table: str) -> list[object]:
    return [hydrate(table, decode_payload(row[0])) for row in connection.execute(f"SELECT payload FROM {table}")]


def open_records(connection: sqlite3.Connection, asset_key: str) -> list[_Open]:
    """The instrument's open quarantined records, oldest first (opened_at, then ID)."""
    resolved: dict[str, Decimal] = {}
    for resolution in _payloads(connection, "quarantine_resolutions"):
        resolved[resolution.record_id] = resolved.get(resolution.record_id, Decimal(0)) + resolution.quantity  # type: ignore[attr-defined]
    records = []
    for table, identity, price, sign in (("short_exposure_liabilities", "liability_id", "sale_price", -1),
                                         ("unmanaged_long_inventory", "record_id", "cost_price", 1)):
        for record in _payloads(connection, table):
            if asset_identity_key(record.asset) != asset_key:  # type: ignore[attr-defined]
                continue
            record_id = getattr(record, identity)
            remaining = record.quantity - resolved.get(record_id, Decimal(0))  # type: ignore[attr-defined]
            if remaining > 0:
                records.append(_Open(record_id, sign, remaining, getattr(record, price), record.multiplier,  # type: ignore[attr-defined]
                                     record.opened_at, record.asset))  # type: ignore[attr-defined]
    return sorted(records, key=lambda record: (record.opened_at, record.record_id))


def _unrecognized(connection: sqlite3.Connection, record_id: str) -> Decimal:
    allocated = sum((a.amount for a in _payloads(connection, "quarantine_fee_allocations")  # type: ignore[attr-defined]
                     if a.record_id == record_id and a.pool == "opening"), Decimal(0))  # type: ignore[attr-defined]
    recognized = sum((r.amount for r in _payloads(connection, "quarantine_fee_recognitions")  # type: ignore[attr-defined]
                      if r.record_id == record_id and r.pool == "opening"), Decimal(0))  # type: ignore[attr-defined]
    return allocated - recognized


def execution_issue(connection: sqlite3.Connection, activity: BrokerActivity) -> str | None:
    """None when ``activity`` is exactly the stored quarantine execution with
    exactly one cash entry equal to its derivation (I1, I11); otherwise why not."""
    row = connection.execute("SELECT payload FROM quarantine_executions WHERE record_id=?", (activity.activity_id,)).fetchone()
    if row is None:
        return "QUARANTINE_EXECUTION_MISSING"
    execution = hydrate("quarantine_executions", decode_payload(row[0]))
    if (execution.side, execution.quantity, execution.price, execution.executed_at) != (
            activity.side, activity.qty, activity.price, activity.transaction_time):
        return "QUARANTINE_EXECUTION_FACTS_CHANGED"
    if execution.raw_sha256 != raw_digest(activity):
        return "BROKER_EVIDENCE_CHANGED"
    return cash_issue(connection, execution)


def cash_issue(connection: sqlite3.Connection, execution: QuarantineExecution) -> str | None:
    rows = connection.execute("SELECT payload FROM cash_ledger WHERE record_id=? OR idempotency_key=?",
                              ("fill:cash:" + execution.activity_id,) * 2).fetchall()
    if len(rows) != 1:
        return "QUARANTINE_EXECUTION_CASH_COUNT"
    if decode_payload(rows[0][0]) != decode_payload(encode_payload(execution_cash(execution))):
        return "ACCOUNTING_DURABLE_ROW_CONFLICT:fill:cash:" + execution.activity_id
    return None


def _find(activities: Sequence[BrokerActivity], activity_id: str) -> BrokerActivity:
    found = [activity for activity in activities if activity.activity_id == activity_id]
    if not found:
        raise ResolveRefused("ACTIVITY_NOT_FOUND")
    if len(found) > 1:
        raise ResolveRefused("ACTIVITY_AMBIGUOUS")
    return found[0]


async def resolve_from_activity(repositories: PersistenceRepositories, broker: BrokerPort, lease: AccountLease,
                                activity_id: str, reason: str, now: datetime) -> ResolveResult:
    """Resolve the instrument's open quarantined records from one verified
    broker execution, oldest first. ``lease`` is the account lease the caller
    holds, so no runtime can trade the account meanwhile."""
    if not reason.strip():
        raise ResolveRefused("REASON_REQUIRED")
    lease.verify()
    activity = _find(await broker.get_activities(activity_type="FILL", since=None), activity_id)
    if activity.activity_type != "FILL":
        raise ResolveRefused("ACTIVITY_NOT_FILL")
    if activity.side is None or activity.qty is None or activity.price is None or activity.transaction_time is None:
        raise ResolveRefused("ACTIVITY_UNPARSABLE")
    if activity.qty <= 0 or activity.price <= 0:
        raise ResolveRefused("ACTIVITY_UNPARSABLE")
    asset_key = broker_symbol_asset_key(activity.symbol)
    digest = raw_digest(activity)
    order_id = str(activity.raw.get("order_id") or "")

    def preflight(connection: sqlite3.Connection) -> tuple[list[_Open], datetime]:
        if connection.execute("SELECT 1 FROM quarantine_executions WHERE record_id=?", (activity_id,)).fetchone() \
                or connection.execute("SELECT 1 FROM quarantine_resolutions WHERE json_extract(payload,'$.activity_id')=?",
                                      (activity_id,)).fetchone():
            raise ResolveRefused("ACTIVITY_ALREADY_USED")
        unresolved = connection.execute("SELECT payload FROM unresolved_broker_executions WHERE record_id=?",
                                        (activity_id,)).fetchone()
        if unresolved is not None and hydrate("unresolved_broker_executions", decode_payload(unresolved[0])).raw_sha256 != digest:
            raise ResolveRefused("BROKER_EVIDENCE_CHANGED")
        if connection.execute("SELECT 1 FROM fills WHERE record_id=? OR broker_fill_id=?", (activity_id, activity_id)).fetchone() \
                or (order_id and (connection.execute("SELECT 1 FROM trade_intents WHERE json_extract(payload,'$.broker_order_id')=?",
                                                     (order_id,)).fetchone()
                                  or connection.execute("SELECT 1 FROM orders WHERE record_id=? OR json_extract(payload,'$.broker_order_id')=?",
                                                        (order_id, order_id)).fetchone())):
            raise ResolveRefused("ACTIVITY_HAS_TRADEPULSE_INTENT")
        records = open_records(connection, asset_key)
        if not records:
            raise ResolveRefused("NO_OPEN_QUARANTINED_RECORD")
        if len({record.sign for record in records}) != 1:
            raise ResolveRefused("QUARANTINE_MIXED_RECORDS")
        if (records[0].sign < 0) != (activity.side == Side.BUY):
            raise ResolveRefused("WRONG_SIDE")
        oldest = records[0].opened_at
        if activity.transaction_time <= oldest:  # type: ignore[operator]
            raise ResolveRefused("ACTIVITY_BEFORE_RECORD")
        if records[0].sign > 0 and activity.qty > sum((record.quantity for record in records), Decimal(0)):  # type: ignore[operator]
            raise ResolveRefused("SALE_EXCEEDS_UNMANAGED_INVENTORY")
        return records, oldest

    records, oldest = await repositories.quarantine_executions.database.run(preflight)
    blocked = corporate_action_blocks(await broker.get_activities(activity_type=None, since=oldest), asset_key, oldest)
    if blocked is not None:
        raise ResolveRefused(f"CORPORATE_ACTION: {blocked}")
    lease.verify()

    kind: Literal["liability_cover", "unmanaged_sale"] = "liability_cover" if records[0].sign < 0 else "unmanaged_sale"
    execution = QuarantineExecution(activity_id=activity_id, kind=kind, asset=records[0].asset,
                                    side=activity.side, quantity=activity.qty, price=activity.price,
                                    executed_at=activity.transaction_time, raw_sha256=digest)
    created = now.isoformat()

    def write(connection: sqlite3.Connection) -> ResolveResult:
        current, _ = preflight(connection)  # re-read under the write lock
        cash = execution_cash(execution)
        connection.execute("INSERT INTO quarantine_executions (record_id,payload,created_at) VALUES (?,?,?)",
                           (activity_id, encode_payload(execution), created))
        connection.execute("INSERT INTO cash_ledger (record_id,payload,created_at,idempotency_key) VALUES (?,?,?,?)",
                           (cash.entry_id, encode_payload(cash), cash.occurred_at.isoformat(), cash.idempotency_key))
        remaining = execution.quantity
        resolutions = []
        for record in current:
            if remaining <= 0:
                break
            covered = min(remaining, record.quantity)
            move = (record.reference_price - execution.price) if record.sign < 0 else (execution.price - record.reference_price)
            resolution = QuarantineResolution(record_id=record.record_id, activity_id=activity_id, quantity=covered,
                                              price=execution.price, realized_pnl=covered * move * record.multiplier,
                                              resolved_at=now)
            key = f"{record.record_id}|{activity_id}"
            connection.execute("INSERT INTO quarantine_resolutions (record_id,resolution_key,payload,created_at) VALUES (?,?,?,?)",
                               (key, key, encode_payload(resolution), created))
            unrecognized = _unrecognized(connection, record.record_id)
            if unrecognized != 0:
                _insert_recognition(connection, recognize_at_resolution(
                    record.record_id, "opening", open_before=record.quantity, covered=covered,
                    unrecognized=unrecognized, event_id=activity_id, now=now), created)
            resolutions.append(resolution)
            remaining -= covered
        surplus = None
        if remaining > 0:  # R4: only a cover can exceed; a sale beyond inventory was refused above
            surplus = UnmanagedLongInventory(record_id="unmanaged:" + activity_id, cover_activity_id=activity_id,
                                             asset=execution.asset, quantity=remaining, cost_price=execution.price,
                                             multiplier=current[0].multiplier, opened_at=execution.executed_at)
            connection.execute("INSERT INTO unmanaged_long_inventory (record_id,cover_activity_id,payload,created_at) VALUES (?,?,?,?)",
                               (surplus.record_id, activity_id, encode_payload(surplus), created))
        unresolved = connection.execute("SELECT 1 FROM unresolved_broker_executions WHERE record_id=?", (activity_id,)).fetchone()
        if unresolved and not connection.execute("SELECT 1 FROM unresolved_execution_outcomes WHERE record_id=?",
                                                 (activity_id,)).fetchone():
            evidence = {"raw_sha256": digest, "execution": encode_payload(execution)}
            outcome = UnresolvedExecutionOutcome(activity_id=activity_id, outcome="quarantine_execution",
                                                 matched_record_id=activity_id,
                                                 evidence_sha256=sha256(encode_payload(evidence).encode()).hexdigest(),
                                                 resolved_at=now)
            connection.execute("INSERT INTO unresolved_execution_outcomes (record_id,payload,created_at) VALUES (?,?,?)",
                               (activity_id, encode_payload(outcome), created))
        event = AuditEvent(
            event_id=f"quarantine_resolve:{activity_id}", event_type="quarantine_resolve", severity="warning",
            message=f"quarantine resolve: {execution.kind} {execution.quantity} {activity.symbol} at {execution.price}",
            occurred_at=now, entity_type="quarantine_execution", entity_id=activity_id,
            details={"reason": reason, "kind": execution.kind, "resolved": [r.record_id for r in resolutions],
                     "surplus": surplus.record_id if surplus else None, "raw_sha256": digest})
        connection.execute("INSERT INTO audit_events (record_id,payload,created_at) VALUES (?,?,?)",
                           (event.event_id, encode_payload(event), created))
        return ResolveResult(tuple(resolutions), surplus)

    return await repositories.quarantine_executions.database.run(write, write=True)


def _insert_recognition(connection: sqlite3.Connection, recognition: QuarantineFeeRecognition, created: str) -> None:
    key = f"{recognition.record_id}|{recognition.event_id}|{recognition.pool}"
    connection.execute("INSERT INTO quarantine_fee_recognitions (record_id,recognition_key,payload,created_at) VALUES (?,?,?,?)",
                       (key, key, encode_payload(recognition), created))
