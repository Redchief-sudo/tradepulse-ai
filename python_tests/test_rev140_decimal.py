"""Rev.140 task 14: no float on any Rev.140 path (spec test 22, I10)."""
from __future__ import annotations

import asyncio
import decimal
import shutil
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rev140_support import Rt
from test_rev140_legacy import SHORT_LOT, _Broker
from test_rev140_resolve import _external, _oversell, _resolve

from tradepulse.broker.port import AccountRef
from tradepulse.execution.sellable import PendingSell, SellableInputs, compute_sellable
from tradepulse.models import DomainValidationError, Side
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.database import QUARANTINE_TABLES
from tradepulse.quarantine.fees import allocate_fee, record_allocations
from tradepulse.quarantine.legacy import adopt_legacy_lot
from tradepulse.runtime_lease import account_lease

FIXTURES = Path(__file__).parent / "fixtures" / "rev140"


@pytest.fixture
def float_trap() -> Iterator[None]:
    """FloatOperation trapped in this thread and in every thread created
    after it (SQLite work runs in asyncio.to_thread workers)."""
    saved = (decimal.getcontext().traps[decimal.FloatOperation], decimal.DefaultContext.traps[decimal.FloatOperation])
    decimal.getcontext().traps[decimal.FloatOperation] = True
    decimal.DefaultContext.traps[decimal.FloatOperation] = True
    try:
        yield
    finally:
        decimal.getcontext().traps[decimal.FloatOperation], decimal.DefaultContext.traps[decimal.FloatOperation] = saved


def _no_float(value: Any, path: str) -> None:
    assert not isinstance(value, float), path
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            _no_float(getattr(value, item.name), f"{path}.{item.name}")
    elif isinstance(value, dict) or hasattr(value, "items"):
        for key, item in value.items():
            _no_float(item, f"{path}[{key}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _no_float(item, f"{path}[{index}]")


async def _all_quarantine_rows(repositories: PersistenceRepositories) -> int:
    seen = 0
    for table in QUARANTINE_TABLES:
        for row in await paginate_all_rows(getattr(repositories, table)):
            record = hydrate(table, row["payload"])
            _no_float(record, table)
            for name in ("quantity", "sale_price", "multiplier", "proceeds", "cost_price", "price", "realized_pnl", "amount"):
                if hasattr(record, name):
                    assert isinstance(getattr(record, name), Decimal), f"{table}.{name}"
            seen += 1
    return seen


@pytest.mark.usefixtures("float_trap")
async def test_no_float_in_rev140_paths(rt: Rt, tmp_path: Path) -> None:
    """An oversell, a cover with a surplus, a fee allocation and a legacy
    adoption, all with FloatOperation trapped; every value read back from
    the ten tables, and every SellableDecision field, is a Decimal."""
    with pytest.raises(decimal.FloatOperation):  # the trap is live here and in worker threads
        Decimal(1.5)  # noqa: RUF032 - the trap must see a float
    with pytest.raises(decimal.FloatOperation):
        await asyncio.to_thread(Decimal, 1.5)
    await _oversell(rt)
    rt.quote("89.90", "90")
    result = await _resolve(rt, await _external(rt, Side.BUY, "5", "big-cover"))
    assert result.surplus is not None
    await record_allocations(rt.repositories, allocate_fee("fee-1", Decimal("-0.05"), [],
                                                           [(result.surplus.record_id, Decimal(450), "opening")]))
    assert await _all_quarantine_rows(rt.repositories) >= 5

    copy = tmp_path / "legacy.db"
    shutil.copyfile(FIXTURES / "legacy_short_open.db", copy)
    database = AsyncSQLiteDatabase(f"sqlite:///{copy}")
    await database.initialize()
    legacy = PersistenceRepositories.create(database)
    with account_lease(AccountRef("alpaca", "SIM0002"), holder="decimal-test", directory=tmp_path) as lease:
        await adopt_legacy_lot(legacy, _Broker(), lease, SHORT_LOT, "decimal guard", datetime(2026, 10, 29, tzinfo=UTC))
    assert await _all_quarantine_rows(legacy) >= 2

    decision = compute_sellable(SellableInputs(broker_quantity=Decimal(10), local_long_quantity=Decimal(10), pending=(
        PendingSell("ord-1", Decimal(3), "i", Decimal(3), Decimal(1)), PendingSell("external", Decimal(1), None, None, Decimal(0)))))
    _no_float(decision, "decision")
    assert all(isinstance(getattr(decision, item.name), Decimal) for item in fields(decision))


@pytest.mark.parametrize("table", ["short_exposure_liabilities", "quarantine_resolutions", "quarantine_fee_allocations"])
def test_a_float_in_a_quarantine_payload_is_refused(table: str) -> None:
    """JSON numbers are floats to the json module; the records refuse them."""
    payloads = {
        "short_exposure_liabilities": {"liability_id": "l", "fill_id": "f", "broker_order_id": "o", "trade_intent_id": "i",
                                       "asset": {"symbol": "AAPL", "asset_class": "equity", "native_asset_id": "alpaca:AAPL"},
                                       "quantity": 2.0, "sale_price": "100", "multiplier": "1", "proceeds": "200",
                                       "opened_at": "2026-10-29T15:00:00+00:00"},
        "quarantine_resolutions": {"record_id": "l", "activity_id": "a", "quantity": "1", "price": 90.5, "realized_pnl": "10",
                                   "resolved_at": "2026-10-29T15:00:00+00:00"},
        "quarantine_fee_allocations": {"fee_canonical_id": "f", "record_id": "l", "pool": "opening", "amount": -0.05},
    }
    with pytest.raises((DomainValidationError, TypeError, ValueError)):
        hydrate(table, payloads[table])
