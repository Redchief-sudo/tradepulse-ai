"""Quarantined inventory (Rev.140, spec §3.2, §3.8): short-exposure liabilities
and unmanaged long inventory. TradePulse never trades them automatically.

``quarantined_positions`` is the one read of open quarantined inventory, used
by the gateway, the monitor, valuation and risk alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from tradepulse.models.market import asset_identity_key
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows


@dataclass(frozen=True, slots=True)
class QuarantinedPosition:
    record_id: str
    kind: Literal["short_liability", "unmanaged_long"]
    asset_key: str
    open_quantity: Decimal  # opening quantity less every resolution, > 0


async def quarantined_positions(repositories: PersistenceRepositories, asset_key: str | None = None) -> list[QuarantinedPosition]:
    resolved: dict[str, Decimal] = {}
    for row in await paginate_all_rows(repositories.quarantine_resolutions):
        resolution = hydrate("quarantine_resolutions", row["payload"])
        resolved[resolution.record_id] = resolved.get(resolution.record_id, Decimal(0)) + resolution.quantity
    positions = []
    for table, kind, identity in (("short_exposure_liabilities", "short_liability", "liability_id"),
                                  ("unmanaged_long_inventory", "unmanaged_long", "record_id")):
        for row in await paginate_all_rows(getattr(repositories, table)):
            record = hydrate(table, row["payload"])
            key = asset_identity_key(record.asset)
            record_id = getattr(record, identity)
            open_quantity = record.quantity - resolved.get(record_id, Decimal(0))
            if open_quantity > 0 and (asset_key is None or key == asset_key):
                positions.append(QuarantinedPosition(record_id, kind, key, open_quantity))  # type: ignore[arg-type]
    return positions
