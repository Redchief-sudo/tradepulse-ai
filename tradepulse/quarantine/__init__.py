"""Quarantined inventory (Rev.140, spec §3.2, §3.3, §3.8): short-exposure liabilities
and unmanaged long inventory. TradePulse never trades them automatically.

``quarantined_positions`` is the one read of open quarantined inventory, used
by the gateway, the monitor, valuation and risk alike. Valuation and exposure
follow spec §3.3's sign table, each at the mark that is adverse for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from tradepulse.models.market import asset_identity_key
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows

BLOCKS = {"short_liability": "short_exposure_liabilities", "unmanaged_long": "unmanaged_long_inventory"}


@dataclass(frozen=True, slots=True)
class QuarantinedPosition:
    record_id: str
    kind: Literal["short_liability", "unmanaged_long"]
    asset_key: str
    sign: Literal[-1, 1]  # -1 a short liability, +1 unmanaged long inventory
    open_quantity: Decimal  # opening quantity less every resolution, > 0
    multiplier: Decimal
    reference_price: Decimal  # a liability's sale price, or the inventory's cost price
    last_confirmed_mark: Decimal | None  # the mark the latest equity snapshot recorded for it

    @property
    def signed_quantity(self) -> Decimal:
        return self.sign * self.open_quantity

    def _marks(self, current: Decimal) -> tuple[Decimal, Decimal]:
        last = self.last_confirmed_mark if self.last_confirmed_mark is not None else current
        return max(last, current), min(last, current)

    def exposure(self, current: Decimal) -> Decimal:
        """Absolute, at the higher mark: the adverse one for risk, either sign."""
        return self.open_quantity * self._marks(current)[0] * self.multiplier

    def equity_value(self, current: Decimal) -> Decimal:
        """Signed, at the mark adverse for equity: a liability at the higher
        mark, unmanaged long inventory at the lower."""
        high, low = self._marks(current)
        return (-self.open_quantity * high if self.sign < 0 else self.open_quantity * low) * self.multiplier

    def unrealized_pnl(self, current: Decimal) -> Decimal:
        move = self.reference_price - current if self.sign < 0 else current - self.reference_price
        return self.open_quantity * move * self.multiplier


async def last_confirmed_marks(repositories: PersistenceRepositories) -> dict[str, Decimal]:
    rows = sorted(await paginate_all_rows(repositories.equity_snapshots), key=lambda row: (row["created_at"], row["record_id"]))
    if not rows:
        return {}
    results = rows[-1]["payload"].get("reconciliation_results") or {}
    return {entry["record_id"]: Decimal(entry["mark"]) for block in BLOCKS.values() for entry in results.get(block, [])}


async def quarantined_positions(repositories: PersistenceRepositories, asset_key: str | None = None) -> list[QuarantinedPosition]:
    resolved: dict[str, Decimal] = {}
    for row in await paginate_all_rows(repositories.quarantine_resolutions):
        resolution = hydrate("quarantine_resolutions", row["payload"])
        resolved[resolution.record_id] = resolved.get(resolution.record_id, Decimal(0)) + resolution.quantity
    marks = await last_confirmed_marks(repositories)
    positions = []
    for table, kind, identity, price, sign in (
            ("short_exposure_liabilities", "short_liability", "liability_id", "sale_price", -1),
            ("unmanaged_long_inventory", "unmanaged_long", "record_id", "cost_price", 1)):
        for row in await paginate_all_rows(getattr(repositories, table)):
            record = hydrate(table, row["payload"])
            key = asset_identity_key(record.asset)
            record_id = getattr(record, identity)
            open_quantity = record.quantity - resolved.get(record_id, Decimal(0))
            if open_quantity > 0 and (asset_key is None or key == asset_key):
                positions.append(QuarantinedPosition(record_id, kind, key, sign, open_quantity,  # type: ignore[arg-type]
                                                     record.multiplier, getattr(record, price), marks.get(record_id)))
    return positions


def signed_quantities(records: list[QuarantinedPosition]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for record in records:
        totals[record.asset_key] = totals.get(record.asset_key, Decimal(0)) + record.signed_quantity
    return totals


def snapshot_blocks(records: list[QuarantinedPosition], marks: dict[str, Decimal]) -> dict[str, list[dict[str, Any]]]:
    """The equity snapshot's blocks: empty lists in normal operation. A
    record without a current broker mark keeps its last confirmed one."""
    blocks: dict[str, list[dict[str, Any]]] = {block: [] for block in BLOCKS.values()}
    for record in records:
        current = marks.get(record.asset_key, record.last_confirmed_mark)
        if current is None:
            current = record.reference_price
        blocks[BLOCKS[record.kind]].append({
            "record_id": record.record_id, "asset_key": record.asset_key, "open_quantity": str(record.open_quantity),
            "mark": str(current), "equity_value": str(record.equity_value(current)),
            "exposure": str(record.exposure(current)), "unrealized_pnl": str(record.unrealized_pnl(current))})
    return blocks
