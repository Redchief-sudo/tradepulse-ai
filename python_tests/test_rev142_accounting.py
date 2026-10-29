"""Rev.142: adopted inventory in run accounting and evidence (T15-T18, T24, T27, T30, T32)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_rev142_adoption import KEY, Rig, rig  # noqa: F401 - the shared runtime fixture

from tradepulse.adoption import Adoption, adoption_pass, write_adoption
from tradepulse.adoption.accounting import adopted_opening_exclusions, adoption_attribution_rows, recompute_attributions
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Fill, PositionLot, Side
from tradepulse.persistence import hydrate, paginate_all_rows
from tradepulse.persistence.codec import encode_payload
from tradepulse.persistence.database import SCHEMA, initialize_identity
from tradepulse.reconciliation.dividend_attribution import Inherited, Member, decide
from tradepulse.verification.evidence import assess, snapshot_database

AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
AT = datetime(2026, 11, 2, 15, tzinfo=UTC)


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    initialize_identity(connection, legacy=False)
    return connection


def _adopt(connection: sqlite3.Connection, *, qty: str, basis: str, mark: str, classification: str = "opening_inventory") -> str:
    adoption = Adoption(adoption_id="adp_1", account_ref="alpaca:SIM", asset_key=KEY, asset=AAPL, classification=classification,  # type: ignore[arg-type]
                        source_id="cp", adopted_quantity=Decimal(qty), broker_avg_entry_price=Decimal(basis),
                        adoption_mark_price=Decimal(mark), adopted_at=AT, stop_loss=Decimal(90), stop_source="pct_fallback",
                        candles_used={}, broker_observation={})
    assert write_adoption(connection, adoption, None)
    return "adopted:adp_1"


def _sell(connection: sqlite3.Connection, lot_id: str, qty: str, price: str, fee: str, fill_id: str = "sell-1") -> None:
    fill = Fill(fill_id, "exit", f"order-{fill_id}", AAPL, Side.SELL, ExecutionMode.PAPER, Decimal(qty), Decimal(price), Decimal(fee),
                Decimal(0), AT + timedelta(days=1), broker_fill_id=f"act-{fill_id}")
    connection.execute("INSERT INTO fills (record_id,broker_fill_id,payload,created_at) VALUES (?,?,?,?)",
                       (fill_id, fill.broker_fill_id, encode_payload(fill), AT.isoformat()))
    row = connection.execute("SELECT payload FROM position_lots WHERE record_id=?", (lot_id,)).fetchone()
    lot = hydrate("position_lots", __import__("json").loads(row[0]))
    updated = PositionLot(**{**{f: getattr(lot, f) for f in lot.__dataclass_fields__},
                             "remaining_quantity": lot.remaining_quantity - Decimal(qty), "closures": {**lot.closures, fill_id: Decimal(qty)}})
    connection.execute("UPDATE position_lots SET payload=? WHERE record_id=?", (encode_payload(updated), lot_id))


def test_generation_exclusion_worked_example() -> None:
    """T16: checkpoint 10 shares, V0 $1,000; sell 4 at $115 with a $2 fee
    (N = $458); 6 left marked at $112: X = 672 + 458 - 1000 = $130."""
    connection = _db()
    lot_id = _adopt(connection, qty="10", basis="100", mark="110")
    opening = {"checkpoint_id": "cp", "positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "10", "market_value": "1000"}]}
    position = SimpleNamespace(qty=Decimal(10), market_value=Decimal(1100))
    assert adopted_opening_exclusions(connection, opening, {KEY: position}) == {KEY: Decimal(100)}  # before any sale
    _sell(connection, lot_id, "4", "115", "2")
    six = SimpleNamespace(qty=Decimal(6), market_value=Decimal(672))
    assert adopted_opening_exclusions(connection, opening, {KEY: six}) == {KEY: Decimal(130)}
    _sell(connection, lot_id, "6", "112", "0", fill_id="sell-2")
    assert adopted_opening_exclusions(connection, opening, {}) == {KEY: Decimal(130)}  # no mark needed at R = 0


def test_other_cash_never_enters_the_exclusion() -> None:
    """T17: a fee linked to another order, a deposit or interest is not in N."""
    connection = _db()
    lot_id = _adopt(connection, qty="10", basis="100", mark="110")
    _sell(connection, lot_id, "10", "115", "0")
    for record_id, relation, debit in (("generation_fee:other", {"order_id": "someone-else"}, "-3"),
                                       ("generation_fee:mine", {"order_id": "order-sell-1"}, "-2")):
        actual = {"authoritative_relationship": relation, "cash_debit": debit}
        connection.execute("INSERT INTO reconciliation_records (record_id,payload,created_at) VALUES (?,?,?)",
                           (record_id, encode_payload({"actual": actual}), AT.isoformat()))
    opening = {"checkpoint_id": "cp", "positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "10", "market_value": "1000"}]}
    # N = 10 x 115 - 2 (its own order's fee only; the other order's 3 is not in it); X = N - V0
    assert adopted_opening_exclusions(connection, opening, {}) == {KEY: Decimal(148)}


def test_two_pnl_measurements_never_summed() -> None:
    """T15: basis $100, mark $110, sale $115 x 10, fee $2: broker basis +$148,
    managed +$48, difference $100. T18: recomputing after a late fee updates
    the row once; replaying changes nothing."""
    connection = _db()
    lot_id = _adopt(connection, qty="10", basis="100", mark="110")
    _sell(connection, lot_id, "10", "115", "2")
    assert recompute_attributions(connection, AT) == 1
    [row] = adoption_attribution_rows(connection)
    assert (row["broker_basis_pnl"], row["managed_pnl"]) == ("148", "48")
    assert recompute_attributions(connection, AT) == 0
    actual = {"authoritative_relationship": {"order_id": "order-sell-1"}, "cash_debit": "-0.05"}
    connection.execute("INSERT INTO reconciliation_records (record_id,payload,created_at) VALUES (?,?,?)",
                       ("generation_fee:late", encode_payload({"actual": actual}), AT.isoformat()))
    assert recompute_attributions(connection, AT) == 1
    [row] = adoption_attribution_rows(connection)
    assert Decimal(row["broker_basis_pnl"]) == Decimal("147.95") and Decimal(row["broker_basis_pnl"]) - Decimal(row["managed_pnl"]) == 100
    assert recompute_attributions(connection, AT) == 0


def test_dividend_on_adopted_inventory_goes_to_the_adopted_bucket() -> None:
    """T30: with E1 established, the adopted holding's dividend is bucket
    'adopted' (excluded through D_excl); without it, excluded_unallocated."""
    income = Member("d", "income", Decimal("2.60"), Decimal(10), Decimal("0.26"), {})
    announcement = __import__("tradepulse.reconciliation.dividend_attribution", fromlist=["Announcement"]).Announcement(
        "a1", "sha", {"id": "a1", "ca_type": "dividend", "ca_sub_type": "cash", "initiating_symbol": "AAPL", "target_symbol": "AAPL",
                      "ex_date": "2026-11-10", "record_date": "2026-11-10", "payable_date": "2026-11-13", "cash": "0.26"})
    common = {"symbol": "AAPL", "credit": AT.date() + timedelta(days=11), "group": [income], "announcements": [announcement],
              "consumed": frozenset(), "lots": [], "quarantined": False, "representation": "gross_and_separate_withholding"}
    adopted = decide(**common, inherited_for=lambda ex: Inherited(True, Decimal(10), bucket="adopted", holder_id="adp_1"))
    assert [(r["bucket"], r["holder_id"], r["amount"]) for r in adopted.rows] == [("adopted", "adp_1", "2.60")]
    unestablished = decide(**common, inherited_for=lambda ex: Inherited(False, None, "x"))
    assert unestablished.attribution_status == "excluded_unallocated"


async def test_no_fill_lookup_and_evidence_through_a_real_exit(rig: Rig, monkeypatch: Any, tmp_path: Path) -> None:  # noqa: F811
    """T32, T24, T27: with any fill lookup of an adoption: key failing the
    test, an adopted lot is adopted, sold through the gateway, settled and
    replayed; the evidence builder lists it as an adoption, with no
    missing-fill error, while open and after full liquidation."""
    real_get = rig.repositories.fills.get

    async def guarded(identifier: str) -> Any:
        assert not identifier.startswith("adoption:"), f"fill lookup of {identifier}"
        return await real_get(identifier)

    monkeypatch.setattr(rig.repositories.fills, "get", guarded)
    for _ in range(2):
        await adoption_pass(rig.repositories, await rig.broker.get_positions(), rig.config, rig.alerts, rig.clock())
    database_path = rig.repositories.fills.database.path
    open_evidence = assess(snapshot_database(database_path), rig.clock().isoformat(), rig.clock(), None)
    assert open_evidence["adoptions"] and not {"lot_missing_opening_fill", "missing_authoritative_fill"} & set(open_evidence["errors"])
    rig.quote("80", "80.10")
    await rig.monitor()
    await rig.settlement.process_pending(force_retry=True)
    await rig.settlement.process_pending(force_retry=True)
    lot = next(hydrate("position_lots", r["payload"]) for r in await paginate_all_rows(rig.repositories.position_lots))
    assert (lot.origin, lot.remaining_quantity) == ("adopted", Decimal(0))
    closed = assess(snapshot_database(database_path), (rig.clock() - timedelta(hours=1)).isoformat(), rig.clock(), None)
    assert closed["adoptions"] == [lot.lot_id]
    assert not {"lot_missing_opening_fill", "missing_authoritative_fill", "fill_allocation_not_conserved"} & set(closed["errors"])


async def test_crypto_adoption_has_its_own_epoch(tmp_path: Path) -> None:
    """T28 (membership): the adopted epoch is written with the adoption; a
    protective SELL joins it; a strategy entry on the asset is refused."""
    import pytest

    from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
    from tradepulse.reconciliation.epochs import AccountingEpochPending, epochs_for, reserve_epoch

    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/c.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    btc = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
    key = "crypto:default:alpaca:BTC/USD"
    adoption = Adoption(adoption_id="adp_btc", account_ref="alpaca:SIM", asset_key=key, asset=btc, classification="unexpected_external",
                        source_id="ep", adopted_quantity=Decimal("0.5"), broker_avg_entry_price=Decimal(60000),
                        adoption_mark_price=Decimal(61000), adopted_at=AT, stop_loss=Decimal(55000), stop_source="pct_fallback",
                        candles_used={}, broker_observation={})
    assert await database.run(lambda c: write_adoption(c, adoption, None), write=True)
    [epoch] = await database.run(lambda c: epochs_for(c, key))
    assert (epoch["origin"], epoch["adoption_id"], epoch["starting_broker_quantity"], epoch["trade_intent_ids"]) == (
        "adopted", "adp_btc", "0.5", [])
    exit_intent = SimpleNamespace(asset=btc, trade_intent_id="exit-1")
    assert await reserve_epoch(repositories, exit_intent, Decimal("0.5"), protective=True, now=AT) == "crypto_epoch:adopted:adp_btc"
    with pytest.raises(AccountingEpochPending, match="CRYPTO_ADOPTED_INVENTORY_OPEN"):
        await reserve_epoch(repositories, SimpleNamespace(asset=btc, trade_intent_id="entry-1"), Decimal("0.5"), protective=False, now=AT)
