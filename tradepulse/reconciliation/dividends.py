"""Dividend cash, recorded exactly once per Alpaca activity (Rev.143, spec §3.1-3.3).

Cash first, attribution after: a valid ``DIV*`` receipt posts its own
``net_amount`` once to the cash ledger, so the generation cash check
balances, whatever happens to attribution. These synchronous helpers run
inside the caller's SQLite transaction, beside the fee and adjustment paths
in ``generation_fees.py``.

Receipt shape (G0, 2026-10-29): the paper account has no dividend history,
so the shape is Alpaca's documented NonTradeActivities schema
(https://docs.alpaca.markets/reference/getaccountactivitiesbyactivitytype-1,
accessed 2026-10-29). A receipt outside it is recorded, but its attribution
is unresolved (DIVIDEND_RECEIPT_SHAPE_UNVERIFIED) until the first real
dividend, the verification event, confirms the shape.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from tradepulse.models import CashLedgerEntry, ReconciliationOutcome, ReconciliationRecord
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.settlement.accounting import _persist
from tradepulse.time import aware_utc

INCOME_TYPES = frozenset({"DIV", "DIVCGL", "DIVCGS", "DIVTXEX"})
WITHHOLDING_TYPES = frozenset({"DIVNRA", "DIVTW", "DIVFT"})
# The documented fields. Required: everything attribution needs; optional:
# the rest. A cash dividend's activity_sub_type is not yet observed, so any
# sub-type makes the shape unverified (a stock dividend is SDIV).
SHAPE_REQUIRED = frozenset({"id", "activity_type", "date", "net_amount", "per_share_amount", "qty", "symbol", "status", "currency"})
SHAPE_OPTIONAL = frozenset({"created_at", "cusip", "group_id", "description"})
VERIFIED_SUB_TYPES: frozenset[str] = frozenset()
NEW_YORK = ZoneInfo("America/New_York")


def _exact(raw: Mapping[str, Any], key: str, identifier: str) -> Decimal | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")  # noqa: TRY004 - one refusal code for every rule
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}") from exc
    if not number.is_finite():
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")
    return number


def economic_type(activity_type: str) -> str:
    if activity_type in INCOME_TYPES:
        return "dividend_income"
    if activity_type in WITHHOLDING_TYPES:
        return "dividend_withholding"
    return "dividend_unsupported"


def dividend_receipt(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """The exact receipt of a ``DIV*`` activity, or None for any other type.
    Raises DIVIDEND_RECEIPT_INVALID:{id} for a ``DIV*`` activity that fails a rule (§3.2)."""
    kind = raw.get("activity_type")
    if not isinstance(kind, str) or not kind.startswith("DIV"):
        return None
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("DIVIDEND_RECEIPT_INVALID:<missing id>")
    if raw.get("currency") != "USD" or raw.get("status") != "executed":
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")
    amount = _exact(raw, "net_amount", identifier)
    economic = economic_type(kind)
    if amount is None or (amount == 0 and economic != "dividend_withholding"):
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")  # a withholding record may carry no cash
    symbol = raw.get("symbol")
    from tradepulse.broker.symbols import broker_symbol_asset_key

    if not isinstance(symbol, str) or not symbol.strip() or not broker_symbol_asset_key(symbol).startswith("equity:"):
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")
    qty, rate = _exact(raw, "qty", identifier), _exact(raw, "per_share_amount", identifier)
    credit_date = raw.get("date")
    if credit_date is not None and not isinstance(credit_date, str):
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")
    if raw.get("created_at") is not None:
        occurred_at = aware_utc(raw["created_at"], field_name="dividend_created_at")
    elif credit_date is not None:
        occurred_at = datetime.combine(datetime.fromisoformat(credit_date).date(), time(0), NEW_YORK)
        occurred_at = aware_utc(occurred_at, field_name="dividend_date")
    else:
        raise ValueError(f"DIVIDEND_RECEIPT_INVALID:{identifier}")
    keys = set(raw)
    shape_verified = (SHAPE_REQUIRED <= keys <= SHAPE_REQUIRED | SHAPE_OPTIONAL | {"activity_sub_type"}
                      and raw.get("activity_sub_type") in (None, *VERIFIED_SUB_TYPES))
    return {"activity_id": identifier, "activity_type": kind, "economic_type": economic, "amount": amount,
            "symbol": symbol.upper(), "asset_key": broker_symbol_asset_key(symbol), "qty": qty, "per_share_amount": rate,
            "credit_date": credit_date, "occurred_at": occurred_at, "shape_verified": shape_verified,
            "reversal": (amount < 0) if economic == "dividend_income" else (amount > 0)}


def dividend_cash_entry(raw: Mapping[str, Any]) -> CashLedgerEntry:
    receipt = dividend_receipt(raw)
    if receipt is None:
        raise ValueError("GENERATION_DIVIDEND_NOT_DIVIDEND")
    identifier = "broker:dividend:" + receipt["activity_id"]
    return CashLedgerEntry(identifier, identifier, receipt["amount"], "USD", receipt["occurred_at"],
                           "immutable broker dividend; dividend-accounting-v1")


def _dividend_actual(raw: Mapping[str, Any], checkpoint: Mapping[str, Any] | None) -> dict[str, Any]:
    receipt = dividend_receipt(raw)
    assert receipt is not None
    return {"schema_version": 1, "activity": dict(raw), "activity_sha256": sha256(encode_payload(dict(raw)).encode()).hexdigest(),
            "verification_generation_id": checkpoint["verification_generation_id"] if checkpoint else None,
            "generation_opening_checkpoint_id": checkpoint["checkpoint_id"] if checkpoint else None,
            "economic_type": receipt["economic_type"], "cash_amount": str(receipt["amount"]),
            "cash_entry_id": "broker:dividend:" + receipt["activity_id"], "currency": "USD",
            "occurred_at": receipt["occurred_at"].isoformat()}


def validate_generation_dividend(record: Mapping[str, Any], entry: Mapping[str, Any], *, raw: Mapping[str, Any] | None = None,
                                 checkpoint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    receipt_raw = record["actual"]["activity"]
    receipt = dividend_receipt(receipt_raw)
    if receipt is None:
        raise ValueError("GENERATION_DIVIDEND_UNCLASSIFIED")
    identifier = "generation_dividend:" + receipt["activity_id"]
    if (record["record_id"] != identifier or record["subject_id"] != identifier
            or record["reconciliation_type"] != "dividend" or record["outcome"] != "matched"
            or record["actual"] != _dividend_actual(receipt_raw, checkpoint)
            or entry != decode_payload(encode_payload(dividend_cash_entry(receipt_raw)))
            or (raw is not None and dict(raw) != receipt_raw)):
        raise ValueError("GENERATION_DIVIDEND_RECEIPT_OR_CASH_MISMATCH:" + receipt["activity_id"])
    aware_utc(record["occurred_at"], field_name="dividend_recorded_at")
    return receipt


def _status_payload(receipt: dict[str, Any], now: datetime) -> dict[str, Any]:
    unsupported = receipt["economic_type"] == "dividend_unsupported"
    reason = "DIVIDEND_TYPE_UNSUPPORTED" if unsupported else None if receipt["shape_verified"] else "DIVIDEND_RECEIPT_SHAPE_UNVERIFIED"
    return {"activity_id": receipt["activity_id"], "symbol": receipt["symbol"], "asset_key": receipt["asset_key"],
            "economic_type": receipt["economic_type"], "cash_amount": str(receipt["amount"]), "cash_status": "recorded",
            "attribution_status": "unresolved" if reason else "pending", "entitlement_status": None, "reason": reason,
            "first_observed_at": now.isoformat(), "escalation_deadline": None, "deadline_source": None,
            "alerted_at": None, "verified_alerted_at": None, "updated_at": now.isoformat()}


def persist_generation_dividends(connection: sqlite3.Connection, activities: list[dict[str, Any]], membership: dict[str, Any], *,
                                 now: datetime) -> set[str]:
    """§3.3: for each eligible ``DIV*`` activity, one cash entry, one record
    and a status row, exactly once. A replay re-validates the record and its cash."""
    from .generation_fees import ELIGIBLE_MEMBERSHIPS

    recorded = set()
    checkpoint = membership["checkpoint"]
    for raw in activities:
        receipt = dividend_receipt(raw)
        if receipt is None:
            continue
        status = membership["classifications"].get(raw["id"])
        if status == "unresolved_generation_membership" or status is None:
            raise ValueError("GENERATION_DIVIDEND_MEMBERSHIP_UNRESOLVED:" + raw["id"])
        if status not in ELIGIBLE_MEMBERSHIPS:
            continue
        identifier = "generation_dividend:" + raw["id"]
        entry = dividend_cash_entry(raw)
        old = connection.execute("SELECT payload FROM reconciliation_records WHERE record_id=?", (identifier,)).fetchone()
        if old:
            cash = connection.execute("SELECT payload FROM cash_ledger WHERE record_id=?", (entry.entry_id,)).fetchone()
            if cash is None:
                raise ValueError("GENERATION_DIVIDEND_CASH_MISSING:" + raw["id"])
            validate_generation_dividend(decode_payload(old[0]), decode_payload(cash[0]), raw=raw, checkpoint=checkpoint)
            recorded.add(raw["id"])
            continue
        _persist(connection, "cash_ledger", entry.entry_id, entry, write=True)
        record = ReconciliationRecord(identifier, "dividend", identifier, ReconciliationOutcome.MATCHED,
                                      expected={"immutable_dividend_receipt": True, "cash_recorded_once": True},
                                      actual=_dividend_actual(raw, checkpoint), occurred_at=now)
        connection.execute("INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)",
                           (identifier, encode_payload(record), now.isoformat()))
        connection.execute("INSERT OR IGNORE INTO dividend_attribution_status(record_id,payload,created_at,updated_at) VALUES(?,?,?,?)",
                           (raw["id"], encode_payload(_status_payload(receipt, now)), now.isoformat(), now.isoformat()))
        recorded.add(raw["id"])
    return recorded


def dividend_totals(connection: sqlite3.Connection) -> Decimal:
    """Net of verified-recorded income and withholding cash (never a capital flow)."""
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    checkpoint = load_bound_opening_checkpoint(connection)
    total = Decimal(0)
    for row in connection.execute("SELECT payload FROM reconciliation_records WHERE record_id LIKE 'generation_dividend:%'"):
        record = decode_payload(row[0])
        cash = connection.execute("SELECT payload FROM cash_ledger WHERE record_id=?", (record["actual"]["cash_entry_id"],)).fetchone()
        if cash is None:
            raise ValueError("GENERATION_DIVIDEND_CASH_MISSING")
        total += validate_generation_dividend(record, decode_payload(cash[0]), checkpoint=checkpoint)["amount"]
    return total
