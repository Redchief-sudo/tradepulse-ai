"""Adopted inventory in the verification run's accounting (Rev.142 §3.6.2, §3.7, §3.8)."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from tradepulse.models import AssetClass, asset_identity_key, asset_key_from_broker_symbol, contract_multiplier_of
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload

from . import adoptions


def _adopted_lots(connection: sqlite3.Connection) -> list[Any]:
    lots = [hydrate("position_lots", decode_payload(row[0])) for row in connection.execute("SELECT payload FROM position_lots")]
    return [lot for lot in lots if lot.origin == "adopted"]


def closing_fees(connection: sqlite3.Connection, fill: Any) -> Decimal:
    """The closing fill's own fee plus every broker cash fee linked to its
    order or fill (positive amounts). An adopted asset has one lot, so all
    of a closing fill's fees belong to it."""
    total = fill.fees
    for (payload,) in connection.execute("SELECT payload FROM reconciliation_records WHERE record_id LIKE 'generation_fee:%'"):
        relation = decode_payload(payload)["actual"]["authoritative_relationship"]
        if relation.get("order_id") == fill.order_id or (fill.broker_fill_id and relation.get("fill_id") == fill.broker_fill_id):
            total += -Decimal(decode_payload(payload)["actual"]["cash_debit"])
    return total


def net_closing_proceeds(connection: sqlite3.Connection, lot: Any) -> Decimal:
    """N: net proceeds of the adopted lot's closing fills only, identified by its closures."""
    total = Decimal(0)
    for fill_id, quantity in lot.closures.items():
        row = connection.execute("SELECT payload FROM fills WHERE record_id=?", (fill_id,)).fetchone()
        if row is None:
            raise ValueError("ADOPTED_CLOSING_FILL_MISSING:" + fill_id)
        fill = hydrate("fills", decode_payload(row[0]))
        share = quantity / fill.quantity
        total += quantity * fill.price * contract_multiplier_of(lot.asset) - closing_fees(connection, fill) * share
    return total


def adopted_opening_exclusions(connection: sqlite3.Connection, opening: Mapping[str, Any],
                               positions: Mapping[str, Any]) -> dict[str, Decimal]:
    """X(k) = R(k) x mark(k) + N(k) - V0(k) for each opening-inventory asset
    adopted in this generation. ADOPTION_CHECKPOINT_MISMATCH when the adopted
    quantity is not the checkpoint quantity (a corruption guard)."""
    rows = adoptions(connection)
    checkpoint_positions = {asset_key_from_broker_symbol(AssetClass(p["asset_class"]), p["symbol"]): p for p in opening["positions"]}
    exclusions = {}
    for lot in _adopted_lots(connection):
        adoption = rows.get(lot.adoption_id or "")
        if adoption is None or adoption["classification"] != "opening_inventory" or adoption["source_id"] != opening["checkpoint_id"]:
            continue
        key = asset_identity_key(lot.asset)
        old = checkpoint_positions.get(key)
        if old is None or Decimal(adoption["adopted_quantity"]) != Decimal(old["qty"]):
            raise ValueError("ADOPTION_CHECKPOINT_MISMATCH:" + key)
        remaining = lot.remaining_quantity
        mark_value = Decimal(0)
        if remaining:
            position = positions.get(key)
            if position is None or not position.qty:
                raise ValueError("opening_inventory_mark_missing:" + key)
            mark_value = remaining * position.market_value / position.qty
        exclusions[key] = mark_value + net_closing_proceeds(connection, lot) - Decimal(old["market_value"])
    return exclusions


def recompute_attributions(connection: sqlite3.Connection, now: Any) -> int:
    """§3.6.2: one row per (adoption, closing fill), recomputed from the lot's
    closures and current fees every time; never accumulated."""
    rows = adoptions(connection)
    written = 0
    for lot in _adopted_lots(connection):
        adoption = rows[lot.adoption_id or ""]
        basis, mark = Decimal(adoption["broker_avg_entry_price"]), Decimal(adoption["adoption_mark_price"])
        multiplier = contract_multiplier_of(lot.asset)
        for fill_id, quantity in lot.closures.items():
            fill = hydrate("fills", decode_payload(connection.execute("SELECT payload FROM fills WHERE record_id=?", (fill_id,)).fetchone()[0]))
            fees = closing_fees(connection, fill) * quantity / fill.quantity
            payload = {"adoption_id": lot.adoption_id, "closing_fill_id": fill_id, "qty": str(quantity), "fill_price": str(fill.price),
                       "allocated_fees": str(fees),
                       "broker_basis_pnl": str((fill.price - basis) * quantity * multiplier - fees),
                       "managed_pnl": str((fill.price - mark) * quantity * multiplier - fees)}
            key = f"{lot.adoption_id}:{fill_id}"
            stamp = now.isoformat()
            written += connection.execute(
                "INSERT INTO adoption_attributions (record_id,adoption_id,closing_fill_id,payload,created_at,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at "
                "WHERE adoption_attributions.payload != excluded.payload",
                (key, lot.adoption_id, fill_id, encode_payload(payload), stamp, stamp)).rowcount
    return written


def adoption_attribution_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(decode_payload(r[0])) for r in connection.execute("SELECT payload FROM adoption_attributions ORDER BY record_id")]
