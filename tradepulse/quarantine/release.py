"""Releasing the short-exposure hold (Rev.140, spec §3.7 step 6, R8).

A SHORT_EXPOSURE hold is released only by `reset-integrity`, in the same
transaction as the session transition, and only once every record its
oversell created on the instrument, and every surplus after it, is fully
resolved. Until then it counts as an active hold everywhere.
"""
from __future__ import annotations

import sqlite3

from tradepulse.models import asset_identity_key
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload

from .resolve import open_records


def released_short_exposure_holds(connection: sqlite3.Connection) -> list[str]:
    """Broker order IDs of SHORT_EXPOSURE holds whose exposure is fully
    resolved: the order's liabilities exist and no quarantined record on
    their instruments is still open."""
    released = []
    for row in connection.execute("SELECT record_id FROM integrity_holds WHERE status='short_exposure' ORDER BY record_id").fetchall():
        liabilities = [hydrate("short_exposure_liabilities", decode_payload(r[0])) for r in connection.execute(
            "SELECT payload FROM short_exposure_liabilities WHERE json_extract(payload,'$.broker_order_id')=?", (row[0],))]
        if liabilities and not any(open_records(connection, asset_identity_key(item.asset)) for item in liabilities):
            released.append(row[0])
    return released


def active_hold_count(connection: sqlite3.Connection) -> int:
    total = connection.execute("SELECT COUNT(*) FROM integrity_holds").fetchone()[0]
    return int(total) - len(released_short_exposure_holds(connection))


async def reset_gate_issues(repositories: PersistenceRepositories) -> list[str]:
    """§3.7 step 6's quarantine gates: no open quarantined record (I8), each
    record resolved within its quantity (I5), C1, and each quarantine
    execution's one cash entry with no Fill sharing its ID (I1, I11)."""
    from tradepulse.settlement.accounting import quarantine_cash_issues

    from . import quarantined_positions
    from .fees import check_c1

    issues = [f"QUARANTINE_OPEN: {record.record_id}" for record in await quarantined_positions(repositories)]

    def check(connection: sqlite3.Connection) -> list[str]:
        found = []
        quantities = {}
        for table, identity in (("short_exposure_liabilities", "liability_id"), ("unmanaged_long_inventory", "record_id")):
            for r in connection.execute(f"SELECT payload FROM {table}"):
                record = hydrate(table, decode_payload(r[0]))
                quantities[getattr(record, identity)] = record.quantity
        resolved: dict[str, object] = {}
        for r in connection.execute("SELECT payload FROM quarantine_resolutions"):
            resolution = hydrate("quarantine_resolutions", decode_payload(r[0]))
            resolved[resolution.record_id] = resolved.get(resolution.record_id, 0) + resolution.quantity  # type: ignore[operator]
        for record_id, total in sorted(resolved.items()):
            if record_id not in quantities or total > quantities[record_id]:  # type: ignore[operator]
                found.append(f"INTEGRITY_VIOLATION: I5 {record_id}")
        found.extend(f"{key}: {value}" for key, value in sorted(quarantine_cash_issues(connection).items()))
        return found

    issues.extend(await repositories.quarantine_resolutions.database.run(check))
    issues.extend(f"INTEGRITY_VIOLATION: C1 {item}" for item in await check_c1(repositories))
    return issues
