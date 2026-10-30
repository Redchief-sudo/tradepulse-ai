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


def _split(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Largest remainder in the amount's own smallest unit (at least a cent),
    ties broken by fill ID; the parts sum to the amount exactly."""
    exponent = amount.as_tuple().exponent
    unit = min(Decimal("0.01"), Decimal(1).scaleb(exponent if isinstance(exponent, int) else -2))
    total = sum(weights.values(), Decimal(0))
    units = int(abs(amount) / unit)
    exact = {key: Decimal(units) * weight / total for key, weight in weights.items()}
    floors = {key: int(value) for key, value in exact.items()}
    for key in sorted(exact, key=lambda k: (-(exact[k] - floors[k]), k))[:units - sum(floors.values())]:
        floors[key] += 1
    sign = -1 if amount < 0 else 1
    return {key: sign * count * unit for key, count in floors.items()}


def closing_fee_allocation(connection: sqlite3.Connection, fills: list[Any]) -> dict[str, Decimal]:
    """Each fill's fees (positive amounts): its own fee, plus each broker cash
    fee receipt allocated exactly once. A receipt naming a known fill goes to
    that fill; a receipt naming only an order is split across all of that
    order's fills by quantity (largest remainder), so an order-level fee is
    never counted once per fill."""
    order_ids = {fill.order_id for fill in fills}
    order_fills: dict[str, list[Any]] = {}
    for (payload,) in connection.execute("SELECT payload FROM fills ORDER BY record_id"):
        fill = hydrate("fills", decode_payload(payload))
        if fill.order_id in order_ids:
            order_fills.setdefault(fill.order_id, []).append(fill)
    by_broker_id = {fill.broker_fill_id: fill for group in order_fills.values() for fill in group if fill.broker_fill_id}
    allocation = {fill.fill_id: fill.fees for group in order_fills.values() for fill in group}
    for (payload,) in connection.execute("SELECT payload FROM reconciliation_records WHERE record_id LIKE 'generation_fee:%' ORDER BY record_id"):
        actual = decode_payload(payload)["actual"]
        relation, fee = actual["authoritative_relationship"], -Decimal(actual["cash_debit"])
        named = by_broker_id.get(relation["fill_id"]) if relation.get("fill_id") else None
        if named is not None:
            allocation[named.fill_id] += fee
        elif relation.get("order_id") in order_fills:
            group = order_fills[relation["order_id"]]
            for fill_id, part in _split(fee, {fill.fill_id: fill.quantity for fill in group}).items():
                allocation[fill_id] += part
    return allocation


def _closing_fills(connection: sqlite3.Connection, lot: Any) -> dict[str, Any]:
    fills = {}
    for fill_id in lot.closures:
        row = connection.execute("SELECT payload FROM fills WHERE record_id=?", (fill_id,)).fetchone()
        if row is None:
            raise ValueError("ADOPTED_CLOSING_FILL_MISSING:" + fill_id)
        fills[fill_id] = hydrate("fills", decode_payload(row[0]))
    return fills


def net_closing_proceeds(connection: sqlite3.Connection, lot: Any) -> Decimal:
    """N: net proceeds of the adopted lot's closing fills only, identified by its closures."""
    fills = _closing_fills(connection, lot)
    fees = closing_fee_allocation(connection, list(fills.values()))
    total = Decimal(0)
    for fill_id, quantity in lot.closures.items():
        fill = fills[fill_id]
        total += quantity * fill.price * contract_multiplier_of(lot.asset) - fees[fill.fill_id] * quantity / fill.quantity
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
        fills = _closing_fills(connection, lot)
        allocation = closing_fee_allocation(connection, list(fills.values()))
        for fill_id, quantity in lot.closures.items():
            fill = fills[fill_id]
            fees = allocation[fill.fill_id] * quantity / fill.quantity
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
