"""History completeness after bootstrap (Rev.140, spec §3.1a, TP140-R5-02).

C is never a completeness proof: Alpaca can report an activity late, under
an ID that sorts before C. Every pass therefore:

1. reads activities since the latest proven P1 baseline, and an overlap
   window from C's time - Δ (the full history, once per trading day);
2. classifies every FILL at or before C that nothing accounts for yet as a
   late activity (an outcome or an unresolved row), records it and alerts;
3. proves P1, baseline + Σ quantity activities = the broker position, per
   asset. A failing asset refuses sells (ACTIVITY_HISTORY_UNPROVEN); a
   second consecutive failure latches FILL_HISTORY_INCOMPLETE:<asset>.

The daily full pass also proves cash to the cent and, when both hold,
records a new baseline, so per-pass reads stay bounded.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker.port import BrokerPort
from tradepulse.broker.symbols import broker_symbol_asset_key
from tradepulse.models import ReconciliationOutcome, ReconciliationRecord
from tradepulse.models.quarantine import UnresolvedBrokerExecution, UnresolvedExecutionOutcome
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.codec import encode_payload
from tradepulse.time import aware_utc

from .bootstrap import FillHistoryIncomplete, _decimal, _side, cash_effect, classify_fill, quantity_effect
from .unresolved import fill_cursor

NEW_YORK = ZoneInfo("America/New_York")
HISTORY: Literal["activity_history"] = "activity_history"  # reconciliation_type of every P1 result and baseline
UNPROVEN_ALL = "*"  # a pass whose reads were not proven complete: every asset is unproven


@dataclass(frozen=True, slots=True)
class CompletenessResult:
    late_activity_ids: tuple[str, ...]
    p1_failures: tuple[str, ...]


def activity_id_time(activity_id: str) -> datetime:
    """Alpaca activity IDs begin with the New York time, yyyymmddHHMMSSmmm."""
    stamp = activity_id.split("::", 1)[0]
    local = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=NEW_YORK)
    return (local + timedelta(milliseconds=int(stamp[14:17] or 0))).astimezone(UTC)


async def _read(broker: BrokerPort, **kwargs: Any) -> list[dict[str, Any]]:
    pages: list[Any] = []
    activities = await broker.get_activities(activity_type=None, page_evidence=pages, **kwargs)
    if not pages or not pages[-1]["terminal"] or any(page["terminal"] for page in pages[:-1]):
        raise FillHistoryIncomplete("ACTIVITY_PAGE_UNPROVEN")
    return [dict(activity.raw) for activity in activities]


def conservation_failures(raws: Sequence[Mapping[str, Any]], positions: Mapping[str, Decimal],
                          baseline: Mapping[str, Decimal]) -> list[str]:
    """P1 per asset. An activity whose quantity effect is not modelled makes
    its own asset unproven rather than being ignored."""
    totals: dict[str, Decimal] = dict(baseline)
    unmodelled: set[str] = set()
    for raw in raws:
        try:
            effect = quantity_effect(raw)
        except FillHistoryIncomplete:
            unmodelled.add(broker_symbol_asset_key(str(raw.get("symbol") or "")))
            continue
        if effect is not None:
            totals[effect[0]] = totals.get(effect[0], Decimal(0)) + effect[1]
    mismatched = {key for key in set(totals) | set(positions)
                  if totals.get(key, Decimal(0)) != positions.get(key, Decimal(0))}
    return sorted(mismatched | unmodelled)


async def _records(repositories: PersistenceRepositories, subject: str) -> list[ReconciliationRecord]:
    """In the order they were written: passes can share a timestamp."""
    rows = sorted(await paginate_all_rows(repositories.reconciliation_records), key=lambda row: (row["created_at"], row["record_id"]))
    records = [hydrate("reconciliation_records", row["payload"]) for row in rows]
    return [row for row in records if row.reconciliation_type == HISTORY and row.subject_id == subject]


async def _baseline(repositories: PersistenceRepositories) -> tuple[str, dict[str, Decimal]] | None:
    baselines = await _records(repositories, "baseline")
    if baselines:
        latest = baselines[-1].actual
        return latest["boundary_activity_id"], {key: Decimal(value) for key, value in latest["quantities"].items()}
    rows = await paginate_all_rows(repositories.fill_cursor_bootstrap)
    if not rows:
        return None
    bootstrap = hydrate("fill_cursor_bootstrap", rows[0]["payload"])
    return bootstrap.boundary_activity_id, {key: Decimal(value) for key, value in bootstrap.position_results.items()}


async def history_unproven(repositories: PersistenceRepositories, asset_key: str) -> bool:
    """Read by the gateway: true while the latest pass's P1 failed for the
    asset (ACTIVITY_HISTORY_UNPROVEN)."""
    results = await _records(repositories, "p1")
    return bool(results) and bool({asset_key, UNPROVEN_ALL} & set(results[-1].actual["failures"]))


def full_pass_due(now: datetime, last_full: datetime | None) -> bool:
    """Once per trading day after the close: a weekday at or after 16:05 New
    York, with no full pass yet that New York date."""
    local = now.astimezone(NEW_YORK)
    if local.weekday() >= 5 or (local.hour, local.minute) < (16, 5):
        return False
    return last_full is None or last_full.astimezone(NEW_YORK).date() != local.date()


async def last_full_pass(repositories: PersistenceRepositories) -> datetime | None:
    results = [row for row in await _records(repositories, "p1") if row.actual.get("full")]
    return results[-1].occurred_at if results else None


def _classify_late(connection: sqlite3.Connection, raws: Sequence[Mapping[str, Any]], cursor: str,
                   pass_id: str, now: datetime) -> list[tuple[str, bool]]:
    """In one transaction: every FILL at or before C with no exact match, no
    outcome and no unresolved row gets one now. Returns (id, unresolved)."""
    identity = connection.execute("SELECT created_at FROM verification_identity WHERE singleton=1").fetchone()
    database_created_at = aware_utc(identity[0], field_name="database_created_at")
    late = []
    for raw in raws:
        identifier = str(raw.get("id"))
        if raw.get("activity_type") != "FILL" or identifier > cursor:
            continue
        if (connection.execute("SELECT 1 FROM fills WHERE record_id=? OR broker_fill_id=?", (identifier, identifier)).fetchone()
                or connection.execute("SELECT 1 FROM quarantine_executions WHERE record_id=?", (identifier,)).fetchone()
                or connection.execute("SELECT 1 FROM unresolved_execution_outcomes WHERE record_id=?", (identifier,)).fetchone()
                or connection.execute("SELECT 1 FROM unresolved_broker_executions WHERE record_id=?", (identifier,)).fetchone()):
            continue
        digest = sha256(encode_payload(dict(raw)).encode()).hexdigest()
        classified = classify_fill(connection, raw, database_created_at)
        if classified is None:
            record = UnresolvedBrokerExecution(
                activity_id=identifier, asset_key=broker_symbol_asset_key(str(raw["symbol"])), side=_side(raw),
                quantity=_decimal(raw, "qty"), price=_decimal(raw, "price"),
                transaction_time=aware_utc(raw["transaction_time"], field_name="transaction_time"),
                raw_sha256=digest, first_seen_at=now, pass_id=pass_id)
            connection.execute("INSERT INTO unresolved_broker_executions (record_id,payload,created_at) VALUES (?,?,?)",
                               (identifier, encode_payload(record), now.isoformat()))
        else:
            outcome, matched, evidence = classified
            result = UnresolvedExecutionOutcome(
                activity_id=identifier, outcome=outcome, matched_record_id=matched,
                evidence_sha256=sha256(encode_payload({**evidence, "raw_sha256": digest}).encode()).hexdigest(),
                resolved_at=now)
            connection.execute("INSERT INTO unresolved_execution_outcomes (record_id,payload,created_at) VALUES (?,?,?)",
                               (identifier, encode_payload(result), now.isoformat()))
        note = ReconciliationRecord(
            record_id=f"late_activity:{identifier}", reconciliation_type="late_activity", subject_id=identifier,
            outcome=ReconciliationOutcome.DRIFT_DETECTED, expected={"reported_before_cursor_passed": True},
            actual={"activity_id": identifier, "first_seen_at": now.isoformat(), "cursor": cursor,
                    "classified": "unresolved" if classified is None else classified[0]}, occurred_at=now)
        connection.execute("INSERT OR IGNORE INTO reconciliation_records (record_id,payload,created_at) VALUES (?,?,?)",
                           (note.record_id, encode_payload(note), now.isoformat()))
        late.append((identifier, classified is None))
    return late


async def completeness_pass(repositories: PersistenceRepositories, broker: BrokerPort, alerts: TelegramAlerter, *,
                            now: datetime, full: bool, overlap_days: int) -> CompletenessResult:
    from tradepulse.risk import latch_financial_integrity_block

    from .coordinator import _alert_once, _utc_day

    cursor = await fill_cursor(repositories)
    baseline = await _baseline(repositories)
    if cursor is None or baseline is None:
        return CompletenessResult((), ())
    pass_id = str(uuid4())
    try:
        if full:
            history = await _read(broker)
            reads = history
        else:
            since_baseline = [raw for raw in await _read(broker, after_id=baseline[0]) if str(raw["id"]) > baseline[0]]
            overlap = await _read(broker, since=activity_id_time(cursor) - timedelta(days=overlap_days))
            reads = list({str(raw["id"]): raw for raw in [*overlap, *since_baseline]}.values())
    except FillHistoryIncomplete as exc:
        # An unproven read proves nothing: every asset is unproven until a
        # pass completes, so sells wait. Not an accounting mismatch: no latch.
        unproven = ReconciliationRecord(
            record_id=f"{HISTORY}:p1:{pass_id}", reconciliation_type=HISTORY, subject_id="p1",
            outcome=ReconciliationOutcome.INCOMPLETE_EVIDENCE, expected={"conserved": True},
            actual={"failures": [UNPROVEN_ALL], "full": False, "cursor": cursor, "pass_id": pass_id, "error": exc.reason},
            occurred_at=now)
        await repositories.reconciliation_records.create_once(unproven.record_id, unproven)
        return CompletenessResult((), (UNPROVEN_ALL,))

    late = await repositories.reconciliation_records.database.run(
        lambda connection: _classify_late(connection, reads, cursor, pass_id, now), write=True)
    for identifier, is_unresolved in late:
        await _alert_once(repositories, alerts, event_type="late_activity", subject=identifier, window=_utc_day(now),
                          severity="critical", message=f"Reconciliation: LATE ACTIVITY -- {identifier} was reported after the "
                          f"fill cursor passed its position.", details={"activity_id": identifier}, now=now)
        if is_unresolved:
            await latch_financial_integrity_block(repositories, f"UNRESOLVED_BROKER_EXECUTION: {identifier}", clock=lambda: now)

    positions = {broker_symbol_asset_key(p.symbol): p.qty for p in await broker.get_positions()}
    if full:
        failures = conservation_failures(history, positions, {})
        account = await broker.get_account()
        try:
            cash = sum((cash_effect(raw) for raw in history), Decimal(0))
        except FillHistoryIncomplete:
            cash = None
        if cash != account.cash:
            failures.append("cash")
    else:
        failures = conservation_failures(since_baseline, positions, baseline[1])

    previous = await _records(repositories, "p1")
    previous_failures = set(previous[-1].actual["failures"]) - {UNPROVEN_ALL} if previous else set()
    result = ReconciliationRecord(
        record_id=f"{HISTORY}:p1:{pass_id}", reconciliation_type=HISTORY, subject_id="p1",
        outcome=ReconciliationOutcome.DRIFT_DETECTED if failures else ReconciliationOutcome.MATCHED,
        expected={"conserved": True}, actual={"failures": failures, "full": full, "cursor": cursor, "pass_id": pass_id},
        occurred_at=now)
    await repositories.reconciliation_records.create_once(result.record_id, result)
    for key in failures:
        if key in previous_failures:
            await latch_financial_integrity_block(repositories, f"FILL_HISTORY_INCOMPLETE:{key}", clock=lambda: now)
    if full and not failures and history:
        quantities: dict[str, Decimal] = {}
        for raw in history:
            effect = quantity_effect(raw)
            if effect is not None:
                quantities[effect[0]] = quantities.get(effect[0], Decimal(0)) + effect[1]
        new_baseline = ReconciliationRecord(
            record_id=f"{HISTORY}:baseline:{history[-1]['id']}", reconciliation_type=HISTORY, subject_id="baseline",
            outcome=ReconciliationOutcome.MATCHED, expected={"p1_and_cash_proven": True},
            actual={"boundary_activity_id": str(history[-1]["id"]),
                    "quantities": {key: str(value) for key, value in quantities.items()}}, occurred_at=now)
        await repositories.reconciliation_records.create_once(new_baseline.record_id, new_baseline)
    return CompletenessResult(tuple(identifier for identifier, _ in late), tuple(failures))

