"""Legacy short lots: controlled recovery (Rev.140, spec §3.9, TP140-04).

Code before Rev.140 booked an oversell's excess as a short ``position_lots``
row. Any such open lot latches LEGACY_SHORT_LOT until the operator converts
it, through `quarantine adopt-legacy-lot`, into a ShortExposureLiability
built from the original fill's facts. The lot and the fill are never
rewritten: an append-only ``legacy_lot_conversions`` row names the lot, and
every lot and holding read leaves converted lots out.

Fees: cash fees are allocated only to closure attributions, and a lot that
can be converted has no closures (precondition 3), so it carries no
attributed fee. Nothing is re-allocated at conversion; a fee posting later
on the oversell's order falls on the liability through Task 10's allocator.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from tradepulse.broker.port import BrokerPort
from tradepulse.models import (
    AuditEvent,
    IntegrityHold,
    IntegrityHoldType,
    PositionLot,
    Side,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.models.quarantine import LegacyLotConversion, ShortExposureLiability
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.runtime_lease import AccountLease

from .resolve import raw_digest


class AdoptRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def converted_lot_ids(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(row[0] for row in connection.execute("SELECT lot_id FROM legacy_lot_conversions"))


async def converted_lots(repositories: PersistenceRepositories) -> frozenset[str]:
    return await repositories.legacy_lot_conversions.database.run(converted_lot_ids)


def _open_short_lots(connection: sqlite3.Connection) -> list[PositionLot]:
    converted = converted_lot_ids(connection)
    lots = [hydrate("position_lots", decode_payload(row[0])) for row in connection.execute("SELECT payload FROM position_lots")]
    return sorted((lot for lot in lots if lot.position_side == "short" and lot.status in ("open", "partially_closed")
                   and lot.lot_id not in converted), key=lambda lot: (lot.opened_at, lot.lot_id))


async def find_open_short_lots(repositories: PersistenceRepositories) -> list[PositionLot]:
    return await repositories.position_lots.database.run(_open_short_lots)


async def latch_legacy_short_lots(repositories: PersistenceRepositories, now: datetime) -> bool:
    """I8: an open, unconverted short lot, or any open quarantined record,
    latches FINANCIAL_INTEGRITY_BLOCKED, at startup and on every
    reconciliation pass."""
    from tradepulse.risk import latch_financial_integrity_block

    from . import quarantined_positions

    lots = await find_open_short_lots(repositories)
    if lots:
        ids = ", ".join(lot.lot_id for lot in lots)
        await latch_financial_integrity_block(repositories, f"LEGACY_SHORT_LOT: {ids}", clock=lambda: now)
        return True
    records = await quarantined_positions(repositories)
    if records:
        ids = ", ".join(record.record_id for record in records)
        await latch_financial_integrity_block(repositories, f"QUARANTINED_INVENTORY_OPEN: {ids}", clock=lambda: now)
        return True
    return False


def lot_digest(raw_payload: str) -> str:
    return sha256(raw_payload.encode()).hexdigest()


async def adopt_legacy_lot(repositories: PersistenceRepositories, broker: BrokerPort, lease: AccountLease,
                           lot_id: str, reason: str, now: datetime) -> LegacyLotConversion:
    """Convert one legacy short lot into a liability, in one transaction.
    Refuses, writing nothing, unless all four preconditions of §3.9 hold."""
    if not reason.strip():
        raise AdoptRefused("REASON_REQUIRED")
    lease.verify()

    def evidence_database(connection: sqlite3.Connection) -> bool:
        """Bound to a verification generation, or a preserved soak database.
        Not ``ever_evidence``: any insert into a financial table sets it, so
        every database that ever held a fill has it."""
        row = connection.execute("SELECT generation_id FROM verification_identity WHERE singleton=1").fetchone()
        files = [str(r[2]) for r in connection.execute("PRAGMA database_list")]
        return (row is not None and row[0] is not None) or any("/data/soaks/" in name for name in files)

    def load(connection: sqlite3.Connection) -> tuple[PositionLot, str, object]:
        if evidence_database(connection):  # precondition 4: only ever on a copy
            raise AdoptRefused("EVIDENCE_DATABASE")
        row = connection.execute("SELECT payload FROM position_lots WHERE record_id=?", (lot_id,)).fetchone()
        if row is None:
            raise AdoptRefused("LOT_NOT_FOUND")
        lot = hydrate("position_lots", decode_payload(row[0]))
        if lot.lot_id in converted_lot_ids(connection):
            raise AdoptRefused("LOT_ALREADY_CONVERTED")
        if lot.position_side != "short":
            raise AdoptRefused("LOT_NOT_SHORT")
        if lot.closures or lot.asset_fee_quantities or lot.remaining_quantity != lot.opened_quantity:
            raise AdoptRefused("LOT_HAS_CLOSURES")  # precondition 3: stays held for a reviewed procedure
        fill_row = connection.execute("SELECT payload FROM fills WHERE record_id=?", (lot.originating_fill_id,)).fetchone()
        if fill_row is None:
            raise AdoptRefused("FILL_NOT_FOUND")
        fill = hydrate("fills", decode_payload(fill_row[0]))
        if fill.side != Side.SELL:
            raise AdoptRefused("FILL_NOT_SELL")
        closed = Decimal(0)
        for other in connection.execute("SELECT payload FROM position_lots"):
            candidate = hydrate("position_lots", decode_payload(other[0]))
            if candidate.position_side == "long":
                closed += candidate.closures.get(fill.fill_id, Decimal(0))
        if lot.opened_quantity != fill.quantity - closed:  # precondition 2 (I3 for the original fill)
            raise AdoptRefused("LOT_QUANTITY_MISMATCH")
        if connection.execute("SELECT 1 FROM short_exposure_liabilities WHERE fill_id=?", (fill.fill_id,)).fetchone():
            raise AdoptRefused("LIABILITY_EXISTS")
        return lot, row[0], fill

    lot, raw_lot, fill = await repositories.position_lots.database.run(load)
    activity_id = fill.broker_fill_id  # type: ignore[attr-defined]
    if not activity_id:
        raise AdoptRefused("FILL_ACTIVITY_MISSING")
    found = [a for a in await broker.get_activities(activity_type="FILL", since=None) if a.activity_id == activity_id]
    if len(found) != 1:
        raise AdoptRefused("FILL_ACTIVITY_MISSING")
    [activity] = found
    if (activity.side, activity.qty, activity.price) != (fill.side, fill.quantity, fill.price):  # type: ignore[attr-defined]
        raise AdoptRefused("BROKER_EVIDENCE_CHANGED")
    digest = raw_digest(activity)
    lease.verify()

    multiplier = contract_multiplier_of(lot.asset)
    broker_order_id = fill.order_id  # type: ignore[attr-defined]
    liability = ShortExposureLiability(
        liability_id=f"liability:{fill.fill_id}", fill_id=fill.fill_id, broker_order_id=broker_order_id,  # type: ignore[attr-defined]
        trade_intent_id=fill.trade_intent_id, asset=lot.asset, quantity=lot.opened_quantity,  # type: ignore[attr-defined]
        sale_price=fill.price, multiplier=multiplier, proceeds=lot.opened_quantity * fill.price * multiplier,  # type: ignore[attr-defined]
        opened_at=fill.filled_at)  # type: ignore[attr-defined]
    conversion = LegacyLotConversion(lot_id=lot.lot_id, liability_id=liability.liability_id, lot_digest=lot_digest(raw_lot),
                                     converted_at=now, reason=reason)
    created = now.isoformat()

    def write(connection: sqlite3.Connection) -> LegacyLotConversion:
        current, current_raw, _ = load(connection)  # re-checked under the write lock
        if current_raw != raw_lot:
            raise AdoptRefused("LOT_CHANGED")
        stored = connection.execute("SELECT payload FROM unresolved_broker_executions WHERE record_id=?", (activity_id,)).fetchone()
        if stored is not None and hydrate("unresolved_broker_executions", decode_payload(stored[0])).raw_sha256 != digest:
            raise AdoptRefused("BROKER_EVIDENCE_CHANGED")  # precondition 1
        connection.execute("INSERT INTO short_exposure_liabilities (record_id,fill_id,payload,created_at) VALUES (?,?,?,?)",
                           (liability.liability_id, liability.fill_id, encode_payload(liability), created))
        connection.execute("INSERT INTO legacy_lot_conversions (record_id,lot_id,payload,created_at) VALUES (?,?,?,?)",
                           (conversion.lot_id, conversion.lot_id, encode_payload(conversion), created))
        # The same hold an oversell's final stage writes (§3.2), so release
        # follows one path (release.py).
        hold = IntegrityHold(broker_order_id=broker_order_id, trade_intent_id=liability.trade_intent_id,
                             hold_type=IntegrityHoldType.SHORT_EXPOSURE, reason=f"UNSUPPORTED_SHORT_POSITION: {liability.liability_id}",
                             created_at=now)
        connection.execute("INSERT OR IGNORE INTO integrity_holds (record_id,status,payload,created_at,updated_at) VALUES (?,?,?,?,?)",
                           (broker_order_id, hold.hold_type.value, encode_payload(hold), created, created))
        key = asset_identity_key(current.asset)
        remaining = [lot for lot in (hydrate("position_lots", decode_payload(r[0]))
                                     for r in connection.execute("SELECT payload FROM position_lots"))
                     if asset_identity_key(lot.asset) == key and lot.status in ("open", "partially_closed")
                     and lot.lot_id not in converted_lot_ids(connection)]
        if not remaining:  # the holding was the lot's view; anything else is rebuilt by reconciliation
            connection.execute("DELETE FROM holdings WHERE record_id=?", (key,))
        event = AuditEvent(
            event_id=f"legacy_lot_conversion:{lot.lot_id}", event_type="legacy_lot_conversion", severity="warning",
            message=f"legacy short lot {lot.lot_id} converted to {liability.liability_id}", occurred_at=now,
            entity_type="legacy_lot_conversion", entity_id=lot.lot_id,
            details={"reason": reason, "liability_id": liability.liability_id, "lot_digest": conversion.lot_digest,
                     "raw_sha256": digest, "quantity": str(liability.quantity)})
        connection.execute("INSERT INTO audit_events (record_id,payload,created_at) VALUES (?,?,?)",
                           (event.event_id, encode_payload(event), created))
        return conversion

    return await repositories.legacy_lot_conversions.database.run(write, write=True)
