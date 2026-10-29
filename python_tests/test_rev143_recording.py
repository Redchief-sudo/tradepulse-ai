"""Rev.143 task 1: dividend receipts and recording (spec §3.1-3.3, §3.10; T1-T5, T20, T23)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradepulse.persistence import AsyncSQLiteDatabase
from tradepulse.persistence.codec import decode_payload
from tradepulse.persistence.database import SCHEMA, DatabaseError, initialize_identity
from tradepulse.reconciliation.dividends import dividend_receipt, validate_generation_dividend
from tradepulse.reconciliation.generation_fees import generation_adjustment_totals, persist_generation_fees

NOW = datetime(2026, 11, 13, 15, tzinfo=UTC)


def div(identifier: str = "div-1", **fields: Any) -> dict[str, Any]:
    """Alpaca's documented NonTradeActivities shape (accessed 2026-10-29)."""
    return {"id": identifier, "activity_type": "DIV", "date": "2026-11-13", "created_at": "2026-11-13T09:00:00-05:00",
            "net_amount": "2.60", "per_share_amount": "0.26", "qty": "10", "symbol": "AAPL", "cusip": "037833100",
            "status": "executed", "currency": "USD", **fields}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    initialize_identity(connection, legacy=False)
    yield connection
    connection.close()


def membership(raw: list[dict[str, Any]], status: str = "in_generation") -> dict[str, Any]:
    return {"checkpoint": None, "classifications": {row["id"]: status for row in raw}, "population_hash": "proof"}


def payloads(db: sqlite3.Connection, table: str) -> list[Any]:
    return [decode_payload(row["payload"]) for row in db.execute("SELECT payload FROM " + table)]


def test_a_dividend_records_cash_a_record_and_a_pending_status(db: sqlite3.Connection) -> None:
    """T1: one ledger entry, one record, one pending status."""
    raw = div()
    persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    [cash] = payloads(db, "cash_ledger")
    assert (cash["entry_id"], cash["amount"], cash["occurred_at"]) == ("broker:dividend:div-1", "2.60", "2026-11-13T14:00:00+00:00")
    [record] = payloads(db, "reconciliation_records")
    assert (record["record_id"], record["reconciliation_type"], record["actual"]["economic_type"]) == (
        "generation_dividend:div-1", "dividend", "dividend_income")
    [status] = payloads(db, "dividend_attribution_status")
    assert (status["cash_status"], status["attribution_status"], status["first_observed_at"]) == ("recorded", "pending", NOW.isoformat())
    assert validate_generation_dividend(record, cash, raw=raw)["amount"] == Decimal("2.60")
    assert generation_adjustment_totals(db)["dividend_income"] == Decimal("2.60")
    assert generation_adjustment_totals(db)["capital_flows"] == 0  # never a capital flow


def test_replay_changes_nothing_and_a_changed_receipt_is_refused(db: sqlite3.Connection) -> None:
    """T2."""
    raw = div()
    for _ in range(3):
        persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    assert len(payloads(db, "cash_ledger")) == len(payloads(db, "reconciliation_records")) == 1
    changed = div(net_amount="2.61")
    with pytest.raises(ValueError, match="GENERATION_DIVIDEND_RECEIPT_OR_CASH_MISMATCH:div-1"):
        persist_generation_fees(db, [changed], membership([changed]), now=NOW)


@pytest.mark.parametrize("fields", [
    {"currency": "CAD"}, {"status": "correct"}, {"status": "canceled"}, {"net_amount": 2.6}, {"net_amount": "0"},
    {"net_amount": None}, {"net_amount": "two"}, {"symbol": None}, {"symbol": "BTC/USD"}, {"id": None},
    {"qty": 10}, {"created_at": None, "date": None},
])
def test_an_invalid_receipt_writes_nothing(db: sqlite3.Connection, fields: dict[str, Any]) -> None:
    """T3: each field rule."""
    raw = div(**fields)
    # a missing id is refused by the shared activity loop before any receipt is read
    with pytest.raises(ValueError, match="DIVIDEND_RECEIPT_INVALID" if raw["id"] else "activity_id is required"):
        persist_generation_fees(db, [raw], membership([{"id": raw["id"] or "x"}]), now=NOW)
    assert payloads(db, "cash_ledger") == payloads(db, "dividend_attribution_status") == []


@pytest.mark.parametrize("kind", ["DIVROC", "DIVFEE", "DIVNEW"])
def test_unsupported_types_record_cash_and_are_unresolved(db: sqlite3.Connection, kind: str) -> None:
    """T4 (the alert, once per activity, is sent by the attribution pass)."""
    raw = div(activity_type=kind, net_amount="-0.50" if kind == "DIVFEE" else "1.00")
    persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    [status] = payloads(db, "dividend_attribution_status")
    assert (status["economic_type"], status["attribution_status"], status["reason"]) == (
        "dividend_unsupported", "unresolved", "DIVIDEND_TYPE_UNSUPPORTED")
    assert len(payloads(db, "cash_ledger")) == 1


def test_pre_run_dividends_are_not_recorded_and_unresolved_membership_raises(db: sqlite3.Connection) -> None:
    """T5."""
    raw = div()
    persist_generation_fees(db, [raw], membership([raw], "pre_generation"), now=NOW)
    assert payloads(db, "cash_ledger") == []
    with pytest.raises(ValueError, match="GENERATION_DIVIDEND_MEMBERSHIP_UNRESOLVED:div-1"):
        persist_generation_fees(db, [raw], membership([raw], "unresolved_generation_membership"), now=NOW)


def test_withholding_signs_and_reversals() -> None:
    assert dividend_receipt(div(activity_type="DIVNRA", net_amount="-0.39"))["economic_type"] == "dividend_withholding"  # type: ignore[index]
    assert dividend_receipt(div(activity_type="DIVNRA", net_amount="0"))["amount"] == 0  # type: ignore[index]
    assert dividend_receipt(div(net_amount="-2.60"))["reversal"]  # type: ignore[index]
    assert dividend_receipt({"id": "f", "activity_type": "FEE"}) is None


@pytest.mark.parametrize(("fields", "verified"), [
    ({}, True), ({"group_id": "g"}, True), ({"activity_sub_type": "SDIV"}, False), ({"surprise": "x"}, False),
    ({"per_share_amount": None}, False),
])
def test_receipt_shape_is_checked_against_the_documented_fields(db: sqlite3.Connection, fields: dict[str, Any], verified: bool) -> None:
    """T23: a shape outside Alpaca's documented one is recorded but unresolved."""
    raw = {key: value for key, value in div(**fields).items() if value is not None}
    persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    [status] = payloads(db, "dividend_attribution_status")
    assert (status["reason"] is None) == verified
    if not verified:
        assert (status["attribution_status"], status["reason"]) == ("unresolved", "DIVIDEND_RECEIPT_SHAPE_UNVERIFIED")
    assert len(payloads(db, "cash_ledger")) == 1


@pytest.mark.parametrize("table", ["corporate_action_announcements", "dividend_attributions"])
async def test_announcements_and_attributions_are_immutable(tmp_path: Path, table: str) -> None:
    """T20."""
    path = tmp_path / "d.db"
    await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()
    with sqlite3.connect(path) as connection:
        if table == "corporate_action_announcements":
            connection.execute("INSERT INTO corporate_action_announcements VALUES ('a|s','a','s','AAPL','{}','t')")
        else:
            connection.execute("INSERT INTO dividend_attributions VALUES ('r','d','income','strategy','lot','{}','t')")
        for statement in (f"UPDATE {table} SET payload='x'", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
        connection.execute(f"DROP TRIGGER quarantine_no_delete_{table}")
    with pytest.raises(DatabaseError, match=f"SCHEMA_TRIGGER_MISSING:quarantine_no_delete_{table}"):
        await AsyncSQLiteDatabase(f"sqlite:///{path}").initialize()
