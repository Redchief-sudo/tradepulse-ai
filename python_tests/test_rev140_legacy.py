"""Rev.140 task 12: legacy short lots and corporate actions (spec §3.9, §3.10, tests 17, 18)."""
from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from rev140_support import Rt

from tradepulse.broker import AlpacaActivity
from tradepulse.broker.port import AccountRef
from tradepulse.models import SessionState, Side
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.quarantine import quarantined_positions, signed_quantities
from tradepulse.quarantine.corporate_actions import corporate_action_blocks
from tradepulse.quarantine.fees import check_c1
from tradepulse.quarantine.legacy import (
    AdoptRefused,
    adopt_legacy_lot,
    converted_lots,
    find_open_short_lots,
    latch_legacy_short_lots,
)
from tradepulse.risk import load_session
from tradepulse.runtime_lease import AccountLease, account_lease
from tradepulse.settlement.accounting import accounting_issues, replay_accounting

FIXTURES = Path(__file__).parent / "fixtures" / "rev140"
SHORT_LOT = "00000000-0000-0000-0000-000000000002"
KEY = "equity:default:alpaca:AAPL"
NOW = datetime(2026, 10, 29, 15, tzinfo=UTC)
FILL_AT = datetime(2026, 9, 14, 14, 2, tzinfo=UTC)


class _Broker:
    """Broker activities only: the oversell's FILL, as the broker reports it."""

    def __init__(self, *, price: str = "110") -> None:
        raw = {"id": "act-2", "activity_type": "FILL", "symbol": "AAPL", "side": "sell", "qty": "12", "price": price,
               "transaction_time": FILL_AT.isoformat(), "order_id": "ord-2"}
        self.activities = [AlpacaActivity("act-2", "FILL", "AAPL", Side.SELL, Decimal(12), Decimal(price), FILL_AT, raw)]

    async def get_activities(self, *args: Any, **kwargs: Any) -> list[AlpacaActivity]:
        return list(self.activities)


async def _copy(tmp_path: Path, name: str = "legacy_short_open.db") -> tuple[Path, PersistenceRepositories]:
    path = tmp_path / name
    shutil.copyfile(FIXTURES / name, path)
    database = AsyncSQLiteDatabase(f"sqlite:///{path}")
    await database.initialize()
    return path, PersistenceRepositories.create(database)


def _rows(path: Path, table: str) -> list[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT record_id, payload FROM {table} ORDER BY record_id").fetchall()


@pytest.fixture
def lease(tmp_path: Path) -> Iterator[AccountLease]:
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="adopt-test", directory=tmp_path) as held:
        yield held


async def test_legacy_short_lot_latched_and_adopted(tmp_path: Path, lease: AccountLease) -> None:
    """Spec test 18: the old code's short lot latches; adoption creates the
    liability and a conversion row; lot and fill bytes are unchanged; I2 and I3 hold."""
    path, repositories = await _copy(tmp_path)
    assert await accounting_issues(repositories) == {}  # old-code ledgers verify under Rev.140
    assert [lot.lot_id for lot in await find_open_short_lots(repositories)] == [SHORT_LOT]
    assert await latch_legacy_short_lots(repositories, NOW)
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert session.financial_integrity_reason == f"LEGACY_SHORT_LOT: {SHORT_LOT}"
    lots_before, fills_before = _rows(path, "position_lots"), _rows(path, "fills")

    conversion = await adopt_legacy_lot(repositories, _Broker(), lease, SHORT_LOT, "pre-Rev.140 oversell", NOW)

    assert (conversion.lot_id, conversion.liability_id) == (SHORT_LOT, "liability:fill-2")
    [liability] = [hydrate("short_exposure_liabilities", r["payload"]) for r in await paginate_all_rows(repositories.short_exposure_liabilities)]
    assert (liability.fill_id, liability.broker_order_id, liability.quantity, liability.sale_price, liability.opened_at) == (
        "fill-2", "ord-2", Decimal(2), Decimal(110), FILL_AT)
    assert (_rows(path, "position_lots"), _rows(path, "fills")) == (lots_before, fills_before)
    assert await converted_lots(repositories) == frozenset({SHORT_LOT})
    assert await find_open_short_lots(repositories) == []
    assert await accounting_issues(repositories) == {}  # I3: 10 closed + liability 2 = the fill's 12
    assert signed_quantities(await quarantined_positions(repositories)) == {KEY: Decimal(-2)}  # I2 with no open lot
    assert await repositories.holdings.get(KEY) is None
    [hold] = await paginate_all_rows(repositories.integrity_holds)
    assert (hold["record_id"], hold["status"]) == ("ord-2", "short_exposure")
    assert await latch_legacy_short_lots(repositories, NOW)  # I8: the liability keeps the latch
    with pytest.raises(AdoptRefused, match="LOT_ALREADY_CONVERTED"):
        await adopt_legacy_lot(repositories, _Broker(), lease, SHORT_LOT, "again", NOW)


async def test_adopt_reallocates_existing_fees_c1_holds(tmp_path: Path, lease: AccountLease) -> None:
    """A convertible lot has no closures, so no fee is attributed to it; the
    oversell's attributions keep their bytes and C1 holds from the conversion on."""
    path, repositories = await _copy(tmp_path)
    attributions = _rows(path, "trade_attributions")
    assert attributions  # the long closure the oversell made
    await adopt_legacy_lot(repositories, _Broker(), lease, SHORT_LOT, "pre-Rev.140 oversell", NOW)
    assert await check_c1(repositories) == []
    assert _rows(path, "trade_attributions") == attributions


async def _refused(tmp_path: Path, lease: AccountLease, reason: str, *, name: str = "legacy_short_open.db",
                   broker: Any = None, prepare: Any = None) -> None:
    path, repositories = await _copy(tmp_path, name)
    if prepare is not None:
        prepare(path)
    tables = ("short_exposure_liabilities", "legacy_lot_conversions", "integrity_holds", "holdings", "audit_events")
    before = {table: _rows(path, table) for table in tables}
    lot = next(lot.lot_id for lot in [hydrate("position_lots", r["payload"]) for r in await paginate_all_rows(repositories.position_lots)]
               if lot.position_side == "short")
    with pytest.raises(AdoptRefused) as refused:
        await adopt_legacy_lot(repositories, broker or _Broker(), lease, lot, "test", NOW)
    assert refused.value.reason == reason
    assert {table: _rows(path, table) for table in tables} == before


async def test_adopt_refuses_lot_with_closures(tmp_path: Path, lease: AccountLease) -> None:
    await _refused(tmp_path, lease, "LOT_HAS_CLOSURES", name="legacy_short_closed.db")


async def test_adopt_refuses_evidence_database(tmp_path: Path, lease: AccountLease) -> None:
    def mark(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE verification_identity SET generation_id='gen-1' WHERE singleton=1")

    await _refused(tmp_path, lease, "EVIDENCE_DATABASE", prepare=mark)


async def test_adopt_refuses_missing_or_changed_broker_evidence(tmp_path: Path, lease: AccountLease) -> None:
    empty = _Broker()
    empty.activities = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    await _refused(tmp_path / "a", lease, "FILL_ACTIVITY_MISSING", broker=empty)
    await _refused(tmp_path / "b", lease, "BROKER_EVIDENCE_CHANGED", broker=_Broker(price="111"))


async def test_fixture_replays_identically(tmp_path: Path) -> None:
    """Spec test 18: a replay of the fixture changes nothing, every time."""
    path, repositories = await _copy(tmp_path)
    ledgers = ("cash_ledger", "pnl_records", "position_lots", "holdings", "trade_attributions")
    before = {table: _rows(path, table) for table in ledgers}
    assert await replay_accounting(repositories) == 0
    assert await replay_accounting(repositories) == 0
    assert {table: _rows(path, table) for table in ledgers} == before


def _activity(activity_type: str, symbol: str = "AAPL", day: str = "2026-10-29") -> AlpacaActivity:
    raw = {"id": f"{activity_type}-1", "activity_type": activity_type, "symbol": symbol, "date": day}
    return AlpacaActivity(f"{activity_type}-1", activity_type, symbol, None, None, None, None, raw)


@pytest.mark.parametrize(("activity_type", "blocks"), [
    ("SSP", True), ("MA", True), ("NC", True), ("SC", True), ("SSO", True), ("REORG", True), ("OPASN", True),
    ("DIV", True), ("DIVNRA", True), ("SOMETHING_NEW", True), ("FEE", False), ("CFEE", False), ("FILL", False),
])
def test_corporate_action_classification(activity_type: str, blocks: bool) -> None:
    """R9: known corporate actions, instrument distributions and any unknown
    type naming the instrument block; executions and fees do not."""
    since = datetime(2026, 10, 28, tzinfo=UTC)
    assert (corporate_action_blocks([_activity(activity_type)], KEY, since) is not None) == blocks
    assert corporate_action_blocks([_activity(activity_type, symbol="MSFT")], KEY, since) is None
    assert corporate_action_blocks([_activity(activity_type, day="2026-10-27")], KEY, since) is None


async def test_unknown_non_trade_activity_blocks_resolution(rt: Rt, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec test 17 (corporate action) and R9, end to end through `quarantine resolve`."""
    from test_rev140_resolve import _external, _liability, _resolve

    from tradepulse.quarantine.resolve import ResolveRefused

    await _liability(rt, "x", "2")
    rt.quote()
    cover = await _external(rt, Side.BUY, "2", "cover")
    real = rt.broker.get_activities

    async def with_unknown(*args: Any, **kwargs: Any) -> list[AlpacaActivity]:
        rows = await real(*args, **kwargs)
        return [*rows, _activity("SOMETHING_NEW", day=rt.clock().date().isoformat())] \
            if kwargs.get("activity_type", "FILL") is None else rows

    monkeypatch.setattr(rt.broker, "get_activities", with_unknown)
    with pytest.raises(ResolveRefused, match="CORPORATE_ACTION: SOMETHING_NEW SOMETHING_NEW-1"):
        await _resolve(rt, cover)
    assert await paginate_all_rows(rt.repositories.quarantine_executions) == []


async def test_adopt_refuses_a_preserved_soak_database(tmp_path: Path, lease: AccountLease) -> None:
    soaks = tmp_path / "data" / "soaks"
    soaks.mkdir(parents=True)
    await _refused(soaks, lease, "EVIDENCE_DATABASE")
