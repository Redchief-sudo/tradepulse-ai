"""Rev.140 task 4: the durable fill cursor C and unresolved executions (spec §3.1a, K1)."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradepulse.broker import AlpacaActivity
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Fill, SessionState, Side
from tradepulse.models.portfolio import CashLedgerEntry
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.reconciliation import unresolved
from tradepulse.reconciliation.unresolved import (
    fill_cursor,
    open_unresolved,
    record_pass,
    relatch_if_open_unresolved,
    write_outcome_if_proven,
)
from tradepulse.risk import load_session, save_session

AT = datetime(2026, 10, 28, 15, tzinfo=UTC)
AAPL = AssetIdentity(asset_class=AssetClass.EQUITY, symbol="AAPL", native_asset_id="alpaca:AAPL")
KEY = "equity:default:alpaca:AAPL"


async def _repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'r.db'}")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _activity(n: int, qty: str = "2", price: str = "100", side: Side = Side.SELL, at: datetime = AT) -> AlpacaActivity:
    identifier = f"20261028{n:09d}::{n:08d}"
    raw = {"id": identifier, "activity_type": "FILL", "symbol": "AAPL", "side": side.value, "qty": qty,
           "price": price, "transaction_time": at.isoformat()}
    return AlpacaActivity(identifier, "FILL", "AAPL", side, Decimal(qty), Decimal(price), at, raw)


async def _account_for(repositories: PersistenceRepositories, activity: AlpacaActivity, *, price: str | None = None,
                       cash: bool = True) -> None:
    """A local Fill (exact ID) and, optionally, its one cash entry."""
    fill = Fill(fill_id=activity.activity_id, trade_intent_id="i1", order_id="o1", asset=AAPL, side=activity.side,
                execution_mode=ExecutionMode.PAPER, quantity=activity.qty, price=Decimal(price) if price else activity.price,
                fees=Decimal(0), slippage=Decimal(0), filled_at=activity.transaction_time,
                broker_fill_id=activity.activity_id)
    await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)
    if cash:
        key = "fill:cash:" + fill.fill_id
        entry = CashLedgerEntry(key, key, fill.quantity * fill.price, "USD", fill.filled_at, "fill cash")
        await repositories.cash_ledger.create_once(key, entry, unique_value=key)


async def test_external_fill_outlives_lookback_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    """Spec test 4c: an unaccounted fill stays open at any age, and
    reset-integrity refuses while it is open, whatever fill_lookback says."""
    from types import SimpleNamespace

    from tradepulse.session_commands import run_reset_integrity

    repositories = await _repositories(tmp_path)
    result = await record_pass(repositories, [_activity(1)], page_proven=True, pass_id="p1", now=AT)
    assert result.recorded == ("20261028000000001::00000001",)
    assert await fill_cursor(repositories) == "20261028000000001::00000001"
    later = AT + timedelta(hours=25)
    assert [r.activity_id for r in await open_unresolved(repositories, KEY)] == ["20261028000000001::00000001"]
    assert await relatch_if_open_unresolved(repositories, later)
    assert (await load_session(repositories)).state == SessionState.FINANCIAL_INTEGRITY_BLOCKED

    def no_broker(_settings: object) -> object:
        raise AssertionError("refused before any broker call")

    monkeypatch.setattr("tradepulse.session_commands.build_broker", no_broker)
    settings = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'r.db'}")
    with caplog.at_level("ERROR"):
        assert await run_reset_integrity(settings, force=False) == 1  # type: ignore[arg-type]
    assert [r.getMessage() for r in caplog.records] == ["reset_integrity_refused_unresolved_broker_execution"]
    assert (await load_session(repositories)).state == SessionState.FINANCIAL_INTEGRITY_BLOCKED


async def test_outcome_only_from_exact_evidence(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    activity = _activity(1)
    await record_pass(repositories, [activity], page_proven=True, pass_id="p1", now=AT)
    await _account_for(repositories, activity, price="100.01")  # same ID, different price: heuristic-equal only
    assert not await write_outcome_if_proven(repositories, activity, now=AT)
    assert len(await open_unresolved(repositories)) == 1


async def test_outcome_needs_exactly_one_cash_entry_and_unchanged_raw(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    activity = _activity(1)
    await record_pass(repositories, [activity], page_proven=True, pass_id="p1", now=AT)
    await _account_for(repositories, activity, cash=False)
    assert not await write_outcome_if_proven(repositories, activity, now=AT)
    key = "fill:cash:" + activity.activity_id
    await repositories.cash_ledger.create_once(key, CashLedgerEntry(key, key, Decimal(200), "USD", AT, "fill cash"),
                                               unique_value=key)
    changed = AlpacaActivity(activity.activity_id, "FILL", "AAPL", Side.SELL, Decimal(2), Decimal(100), AT,
                             {**activity.raw, "order_id": "rewritten"})
    assert not await write_outcome_if_proven(repositories, changed, now=AT)
    assert await write_outcome_if_proven(repositories, activity, now=AT)
    assert await open_unresolved(repositories) == []
    assert not await write_outcome_if_proven(repositories, activity, now=AT)  # written exactly once


async def test_accounted_fill_is_not_recorded(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    activity = _activity(1)
    await _account_for(repositories, activity)
    result = await record_pass(repositories, [activity], page_proven=True, pass_id="p1", now=AT)
    assert result.recorded == () and result.cursor == activity.activity_id


async def test_crash_between_record_and_advance_leaves_neither(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4d: the row and the cursor move together or not at all."""
    repositories = await _repositories(tmp_path)
    await record_pass(repositories, [_activity(1)], page_proven=True, pass_id="p1", now=AT)

    def crash(*_args: object) -> None:
        raise RuntimeError("crash between record and advance")

    monkeypatch.setattr(unresolved, "_advance_cursor", crash)
    with pytest.raises(Exception, match="crash between record and advance"):
        await record_pass(repositories, [_activity(2)], page_proven=True, pass_id="p2", now=AT)
    assert [r.activity_id for r in await open_unresolved(repositories)] == ["20261028000000001::00000001"]
    assert await fill_cursor(repositories) == "20261028000000001::00000001"


async def test_unproven_page_does_not_advance(tmp_path: Path) -> None:
    """Spec 4e."""
    repositories = await _repositories(tmp_path)
    result = await record_pass(repositories, [_activity(1)], page_proven=False, pass_id="p1", now=AT)
    assert result.cursor is None and result.recorded == ()
    assert await fill_cursor(repositories) is None
    assert await open_unresolved(repositories) == []


async def test_cursor_never_moves_backwards(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    await record_pass(repositories, [_activity(5)], page_proven=True, pass_id="p1", now=AT)
    with pytest.raises(Exception, match="FILL_CURSOR_REGRESSION"):
        await record_pass(repositories, [_activity(3)], page_proven=True, pass_id="p2", now=AT)
    assert await fill_cursor(repositories) == "20261028000000005::00000005"


async def test_startup_relatches_open_unresolved(tmp_path: Path) -> None:
    """Review Focus 3: a previous process recorded the row but its latch call
    was lost; startup re-latches before any lane runs."""
    from tradepulse.models import TradingSession

    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, AT))
    await record_pass(repositories, [_activity(1)], page_proven=True, pass_id="p1", now=AT)
    assert (await load_session(repositories)).state == SessionState.ACTIVE
    assert await relatch_if_open_unresolved(repositories, AT)
    assert (await load_session(repositories)).state == SessionState.FINANCIAL_INTEGRITY_BLOCKED


async def test_nothing_open_means_no_relatch(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    assert not await relatch_if_open_unresolved(repositories, AT)


async def test_k1_every_activity_at_or_before_c_accounted_or_recorded(tmp_path: Path) -> None:
    """50 seeded activities over several passes, some accounted locally,
    some not, one unproven page: after every pass, each FILL at or before C
    has an exact local match or an unresolved row."""
    repositories = await _repositories(tmp_path)
    rng = random.Random(140)
    activities = [_activity(n, qty=str(rng.randint(1, 9)), side=rng.choice([Side.BUY, Side.SELL])) for n in range(1, 51)]
    accounted = set()
    for activity in activities:
        if rng.random() < 0.6:
            await _account_for(repositories, activity)
            accounted.add(activity.activity_id)
    position = 0
    for pass_number, size in enumerate([7, 0, 13, 9, 21]):
        page = activities[position:position + size]
        proven = pass_number != 3
        await record_pass(repositories, page, page_proven=proven, pass_id=f"p{pass_number}", now=AT)
        if proven:
            position += size
        cursor = await fill_cursor(repositories)
        recorded = {r.activity_id for r in await open_unresolved(repositories)}
        for activity in activities:
            if cursor is not None and activity.activity_id <= cursor:
                assert activity.activity_id in accounted or activity.activity_id in recorded
            else:
                assert activity.activity_id not in recorded
    assert position == 50 - 9


def _broker_reads(lookback: list[AlpacaActivity], after_cursor: list[AlpacaActivity], *, terminal: bool = True):
    from unittest.mock import AsyncMock

    calls: list[str | None] = []

    async def get_activities(activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100,
                             *, after_id: str | None = None, page_evidence: list | None = None) -> list[AlpacaActivity]:
        calls.append(after_id)
        if after_id is None:
            return lookback
        if page_evidence is not None:
            page_evidence.append({"terminal": terminal})
        return [a for a in after_cursor if a.activity_id > after_id]

    broker = AsyncMock()
    broker.get_activities.side_effect = get_activities
    return broker, calls


async def _reconcile(repositories: PersistenceRepositories, broker: object) -> tuple[int, int, int]:
    from unittest.mock import AsyncMock

    from test_settlement_engine import _no_op_alerter

    from tradepulse.reconciliation.coordinator import _reconcile_fills

    return await _reconcile_fills(repositories, broker, AsyncMock(), _no_op_alerter(), AT, timedelta(days=1))  # type: ignore[arg-type]


async def test_reconcile_records_an_external_fill_after_the_cursor_and_latches(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    accounted, external = _activity(1), _activity(2)
    await _account_for(repositories, accounted)
    await record_pass(repositories, [accounted], page_proven=True, pass_id="p0", now=AT)
    broker, calls = _broker_reads([accounted], [accounted, external])
    await _reconcile(repositories, broker)
    assert calls == [None, accounted.activity_id]
    assert [r.activity_id for r in await open_unresolved(repositories)] == [external.activity_id]
    assert await fill_cursor(repositories) == external.activity_id
    assert (await load_session(repositories)).state == SessionState.FINANCIAL_INTEGRITY_BLOCKED


async def test_reconcile_does_not_advance_on_an_unproven_page(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    accounted, external = _activity(1), _activity(2)
    await _account_for(repositories, accounted)
    await record_pass(repositories, [accounted], page_proven=True, pass_id="p0", now=AT)
    broker, _ = _broker_reads([accounted], [accounted, external], terminal=False)
    await _reconcile(repositories, broker)
    assert await fill_cursor(repositories) == accounted.activity_id
    assert await open_unresolved(repositories) == []


async def test_reconcile_writes_the_outcome_once_exact_evidence_exists(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    late = _activity(1)
    await record_pass(repositories, [late], page_proven=True, pass_id="p0", now=AT)
    await _account_for(repositories, late)  # settled later, with its one cash entry
    broker, _ = _broker_reads([late], [late])
    await _reconcile(repositories, broker)
    assert await open_unresolved(repositories) == []


async def test_reconcile_skips_the_cursor_before_bootstrap(tmp_path: Path) -> None:
    repositories = await _repositories(tmp_path)
    broker, calls = _broker_reads([], [])
    await _reconcile(repositories, broker)
    assert calls == [None] and await fill_cursor(repositories) is None
