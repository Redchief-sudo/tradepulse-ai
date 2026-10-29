"""Bootstrap of the fill cursor C from complete broker history (Rev.140, spec §3.1a, TP140-R5-01).

C never comes into existence by assumption. ``bootstrap_fill_cursor`` reads
the account, positions and every activity from inception, proves the
history complete (inception, per-asset quantity P1, cash to the cent),
classifies every FILL, and writes every outcome, every unresolved row, the
bootstrap record and C in one transaction. Any failure writes nothing.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Any, Literal

from tradepulse.broker.port import BrokerPort
from tradepulse.broker.symbols import broker_symbol_asset_class, broker_symbol_asset_key
from tradepulse.models import AssetClass, Side
from tradepulse.models.quarantine import FillCursorBootstrap, UnresolvedBrokerExecution, UnresolvedExecutionOutcome
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.runtime_lease import AccountLease
from tradepulse.time import aware_utc

from .unresolved import FILL_CURSOR_RECORD_ID, _advance_cursor

# Activities that move cash only, whatever quantity field they carry: a
# dividend's qty is the eligible shares, not a change in the position.
CASH_ONLY_TYPES = frozenset({
    "CSD", "CSW", "JNLC", "JNLD", "INT", "INTNRA", "INTTW", "FEE", "PTC", "PTR", "REB", "CIL", "MEM", "WH",
    "DIV", "DIVCGL", "DIVCGS", "DIVFEE", "DIVFT", "DIVNRA", "DIVROC", "DIVTW", "DIVTXEX",
})
FUNDING_TYPES = frozenset({"CSD", "JNLC"})
_CENT = Decimal("0.01")


class FillHistoryIncomplete(Exception):
    """FILL_HISTORY_INCOMPLETE and the other bootstrap refusals. Nothing is written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def account_ref_key(lease: AccountLease) -> str:
    return lease.ref.key


def _decimal(raw: Mapping[str, Any], name: str) -> Decimal:
    value = raw.get(name)
    if value is None or isinstance(value, (bool, float)):
        raise FillHistoryIncomplete(f"INVENTORY_EVIDENCE_INVALID: {raw.get('id')} {name}")
    return Decimal(str(value))


def multiplier_of(symbol: str) -> Decimal:
    return Decimal(100) if broker_symbol_asset_class(symbol) == AssetClass.OPTION else Decimal(1)


def quantity_effect(raw: Mapping[str, Any]) -> tuple[str, Decimal] | None:
    """(asset key, signed quantity) for a quantity-changing activity, None for
    a cash-only one. A type that changes quantity in a way not modelled here
    refuses instead of being ignored."""
    kind = str(raw.get("activity_type") or "")
    if kind == "FILL":
        signed = _decimal(raw, "qty") if raw.get("side") in ("buy", "buy_to_cover") else -_decimal(raw, "qty")
        return broker_symbol_asset_key(str(raw["symbol"])), signed
    if kind == "CFEE":
        quantity = Decimal(str(raw.get("qty") or 0))
        return (broker_symbol_asset_key(str(raw["symbol"])), quantity) if quantity else None
    if kind in CASH_ONLY_TYPES or not raw.get("symbol"):
        return None
    raise FillHistoryIncomplete(f"FILL_HISTORY_INCOMPLETE: unmodelled {kind}")


def cash_effect(raw: Mapping[str, Any]) -> Decimal:
    """A FILL moves qty x price x multiplier, rounded to the cent per fill
    (measured exact on the paper account, 2026-10-28); everything else moves
    its net_amount. An exact half cent cannot be settled and refuses."""
    if raw.get("activity_type") == "FILL":
        notional = _decimal(raw, "qty") * _decimal(raw, "price") * multiplier_of(str(raw["symbol"]))
        if (notional * 100) % 1 == Decimal("0.5"):
            raise FillHistoryIncomplete("FILL_HISTORY_INCOMPLETE: rounding tie")
        rounded = notional.quantize(_CENT, rounding=ROUND_HALF_UP)
        return -rounded if raw.get("side") in ("buy", "buy_to_cover") else rounded
    return Decimal(str(raw.get("net_amount") or 0))


def activity_date(raw: Mapping[str, Any]) -> date:
    stamp = raw.get("transaction_time") or raw.get("created_at")
    return aware_utc(stamp, field_name="activity_time").date() if stamp else date.fromisoformat(str(raw["date"]))


def history_quantities(raws: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for raw in raws:
        effect = quantity_effect(raw)
        if effect is not None:
            totals[effect[0]] += effect[1]
    return dict(totals)


def check_conservation(raws: Sequence[Mapping[str, Any]], positions: Mapping[str, Decimal],
                       baseline: Mapping[str, Decimal] | None = None) -> list[str]:
    """P1: baseline + Σ quantity activities = the broker's position, exactly,
    for every asset either side knows. Returns the assets that fail."""
    totals: dict[str, Decimal] = defaultdict(Decimal, baseline or {})
    for key, quantity in history_quantities(raws).items():
        totals[key] += quantity
    return sorted(key for key in set(totals) | set(positions) if totals.get(key, Decimal(0)) != positions.get(key, Decimal(0)))


def check_inception(raws: Sequence[Mapping[str, Any]], account_created_at: str | None) -> None:
    if not raws or not account_created_at:
        raise FillHistoryIncomplete("FILL_HISTORY_INCOMPLETE: inception")
    created = aware_utc(account_created_at, field_name="account_created_at").date()
    first_cash = next((raw for raw in raws if cash_effect(raw) != 0), None)
    if (activity_date(raws[0]) < created or first_cash is None
            or first_cash.get("activity_type") not in FUNDING_TYPES or cash_effect(first_cash) <= 0):
        raise FillHistoryIncomplete("FILL_HISTORY_INCOMPLETE: inception")


Classification = Literal["matched_fill", "pre_database", "legacy_reconciled"]


def classify_fill(connection: sqlite3.Connection, raw: Mapping[str, Any],
                  database_created_at: datetime) -> tuple[Classification, str, dict[str, Any]] | None:
    """(class, matched record, evidence) for a FILL TradePulse can account
    for, or None for one to record as unresolved (spec §3.1a step 5)."""
    identifier = str(raw["id"])
    at = aware_utc(raw["transaction_time"], field_name="transaction_time")
    side = "buy" if raw.get("side") in ("buy", "buy_to_cover") else "sell"
    row = connection.execute("SELECT payload FROM fills WHERE record_id=? OR broker_fill_id=?", (identifier, identifier)).fetchone()
    if row is not None:
        fill = hydrate("fills", decode_payload(row[0]))
        if (fill.side.value, fill.quantity, fill.price, fill.filled_at) == (side, _decimal(raw, "qty"), _decimal(raw, "price"), at):
            return "matched_fill", fill.fill_id, {"fill": row[0]}
        return None
    if at < database_created_at:
        return "pre_database", "verification_identity", {"database_created_at": database_created_at.isoformat()}
    for record_id, payload in connection.execute(
            "SELECT record_id, payload FROM reconciliation_records WHERE "
            "json_extract(payload,'$.reconciliation_type')='fill' AND json_extract(payload,'$.subject_id')=? "
            "AND json_extract(payload,'$.outcome') IN ('matched','corrected') ORDER BY created_at, record_id",
            (identifier,)):
        return "legacy_reconciled", record_id, {"record_sha256": sha256(payload.encode()).hexdigest()}
    return None


def _evidence_database(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT generation_id FROM verification_identity WHERE singleton=1").fetchone()
    return row is not None and row[0] is not None


async def bootstrap_fill_cursor(repositories: PersistenceRepositories, broker: BrokerPort, lease: AccountLease, *,
                                reason: str, now: datetime) -> FillCursorBootstrap:
    """Requires the account lease, held by the caller for this command (no
    runtime may run). Refuses an evidence database and a second bootstrap."""
    reason = str(reason or "").strip()
    if not reason:
        raise FillHistoryIncomplete("BOOTSTRAP_REASON_REQUIRED")
    try:
        lease.verify()
    except Exception as exc:
        raise FillHistoryIncomplete("ACCOUNT_LEASE_NOT_HELD") from exc
    account_key = account_ref_key(lease)
    database = repositories.fill_cursor_bootstrap.database
    if await database.run(_evidence_database):
        raise FillHistoryIncomplete("BOOTSTRAP_REFUSED_EVIDENCE_DATABASE")
    if await repositories.fill_cursor_bootstrap.find_by_unique(account_key) is not None:
        raise FillHistoryIncomplete("FILL_CURSOR_ALREADY_BOOTSTRAPPED")

    account = await broker.get_account()
    if account.account_number != lease.ref.account_number:
        raise FillHistoryIncomplete("BOOTSTRAP_ACCOUNT_MISMATCH")
    positions = await broker.get_positions()
    pages: list[Any] = []
    activities = await broker.get_activities(activity_type=None, page_evidence=pages)
    if not pages or not pages[-1]["terminal"] or any(page["terminal"] for page in pages[:-1]):
        raise FillHistoryIncomplete("ACTIVITY_PAGE_UNPROVEN")
    raws = [dict(activity.raw) for activity in activities]

    check_inception(raws, account.raw.get("created_at"))
    broker_quantities = {broker_symbol_asset_key(p.symbol): p.qty for p in positions}
    failures = check_conservation(raws, broker_quantities)
    if failures:
        raise FillHistoryIncomplete(f"FILL_HISTORY_INCOMPLETE:{failures[0]}")
    cash = sum((cash_effect(raw) for raw in raws), Decimal(0))
    if cash != account.cash:
        raise FillHistoryIncomplete("FILL_HISTORY_INCOMPLETE:cash")

    def write(connection: sqlite3.Connection) -> FillCursorBootstrap:
        before = sha256(connection.serialize()).hexdigest()
        identity = connection.execute("SELECT created_at FROM verification_identity WHERE singleton=1").fetchone()
        database_created_at = aware_utc(identity[0], field_name="database_created_at")
        for raw in raws:
            if raw.get("activity_type") != "FILL":
                continue
            identifier = str(raw["id"])
            digest = sha256(encode_payload(raw).encode()).hexdigest()
            classified = classify_fill(connection, raw, database_created_at)
            if classified is None:
                record = UnresolvedBrokerExecution(
                    activity_id=identifier, asset_key=broker_symbol_asset_key(str(raw["symbol"])),
                    side=_side(raw), quantity=_decimal(raw, "qty"), price=_decimal(raw, "price"),
                    transaction_time=aware_utc(raw["transaction_time"], field_name="transaction_time"),
                    raw_sha256=digest, first_seen_at=now, pass_id="bootstrap")
                connection.execute("INSERT INTO unresolved_broker_executions (record_id,payload,created_at) VALUES (?,?,?)",
                                   (identifier, encode_payload(record), now.isoformat()))
                continue
            outcome, matched, evidence = classified
            result = UnresolvedExecutionOutcome(
                activity_id=identifier, outcome=outcome, matched_record_id=matched,
                evidence_sha256=sha256(encode_payload({**evidence, "raw_sha256": digest}).encode()).hexdigest(),
                resolved_at=now)
            connection.execute("INSERT INTO unresolved_execution_outcomes (record_id,payload,created_at) VALUES (?,?,?)",
                               (identifier, encode_payload(result), now.isoformat()))
        boundary = str(raws[-1]["id"])
        bootstrap = FillCursorBootstrap(
            account_ref=account_key, boundary_activity_id=boundary, activity_count=len(raws),
            page_sha256s=tuple(str(page["response_hash"]) for page in pages),
            position_results={key: str(value) for key, value in history_quantities(raws).items()},
            cash_result={"broker_cash": str(account.cash), "history_cash": str(cash)},
            database_sha256_before=before, reason=reason, created_at=now)
        connection.execute("INSERT INTO fill_cursor_bootstrap (record_id,account_ref,payload,created_at) VALUES (?,?,?,?)",
                           (account_key, account_key, encode_payload(bootstrap), now.isoformat()))
        _advance_cursor(connection, boundary, "bootstrap", now)
        return bootstrap

    return await database.run(write, write=True)


def _side(raw: Mapping[str, Any]) -> Side:
    return Side.BUY if raw.get("side") in ("buy", "buy_to_cover") else Side.SELL


async def require_bootstrapped(repositories: PersistenceRepositories, account_key: str) -> None:
    """Called at the start of run, scan and monitor: never half-protected."""
    if await repositories.fill_cursor_bootstrap.find_by_unique(account_key) is None \
            or await repositories.broker_activity_cursors.get(FILL_CURSOR_RECORD_ID) is None:
        raise FillHistoryIncomplete("FILL_CURSOR_NOT_BOOTSTRAPPED")
