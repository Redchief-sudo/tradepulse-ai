"""Rev.140 task 5: bootstrap of the fill cursor from complete history (spec §3.1a, tests 4k-4m)."""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_simulated_broker import FEES, T0, _order, _session

from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import ManualClock, SimulatedBroker, _Activity
from tradepulse.models import AssetClass, ExecutionMode, Fill, ReconciliationOutcome, ReconciliationRecord, Side
from tradepulse.models.market import asset_key_from_broker_symbol
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.reconciliation.bootstrap import FillHistoryIncomplete, bootstrap_fill_cursor, require_bootstrapped
from tradepulse.reconciliation.unresolved import fill_cursor, open_unresolved
from tradepulse.runtime_lease import AccountLease, account_lease

DB_CREATED = T0 + timedelta(days=1)  # the database exists from here: earlier fills are pre-database
AAPL_KEY = asset_key_from_broker_symbol(AssetClass.EQUITY, "AAPL")


@contextlib.contextmanager
def _lease(tmp_path: Path) -> Iterator[AccountLease]:
    (tmp_path / "leases").mkdir(mode=0o700, exist_ok=True)
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="test bootstrap", directory=tmp_path / "leases") as lease:
        yield lease


async def _repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PersistenceRepositories:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return DB_CREATED

    with monkeypatch.context() as patch:
        patch.setattr("tradepulse.persistence.database.datetime", _Clock)
        database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'b.db'}")
        await database.initialize()
    return PersistenceRepositories.create(database)


def _quote_all(broker: SimulatedBroker, at: datetime) -> None:
    for symbol, price in (("AAPL", "100"), ("MSFT", "400")):
        broker.set_quote(symbol, AssetClass.EQUITY, Decimal(price), Decimal(price) + Decimal("0.10"), at=at)


async def _history(clock: ManualClock) -> tuple[SimulatedBroker, dict[str, Any]]:
    """Three kinds of FILL and a fee batch: pre-database (AAPL buy before the
    database), matched (AAPL buy), legacy-heuristic (AAPL sell) and
    unaccounted (MSFT buy)."""
    broker = SimulatedBroker(clock=clock, session=_session(), opening_journal=True, fee_scenario=FEES)
    fills: dict[str, Any] = {}
    for name, day, symbol, qty, side in [("pre", 0, "AAPL", "5", Side.BUY), ("matched", 2, "AAPL", "3", Side.BUY),
                                         ("legacy", 3, "AAPL", "2", Side.SELL), ("external", 4, "MSFT", "1", Side.BUY)]:
        clock.set(T0 + timedelta(days=day))
        _quote_all(broker, clock())
        placed = await broker.place_order(_order(symbol, qty, side, coid=name))
        (fills[name],) = [a for a in await broker.get_activities() if a.raw["order_id"] == placed.broker_order_id]
    clock.set(T0 + timedelta(days=5))
    return broker, fills


async def _record_locally(repositories: PersistenceRepositories, fills: dict[str, Any]) -> None:
    exact = fills["matched"]
    fill = Fill(fill_id=exact.activity_id, trade_intent_id="i-matched", order_id=exact.raw["order_id"],
                asset=hydrate_asset("AAPL"), side=exact.side, execution_mode=ExecutionMode.PAPER, quantity=exact.qty,
                price=exact.price, fees=Decimal(0), slippage=Decimal(0), filled_at=exact.transaction_time,
                broker_fill_id=exact.activity_id)
    await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)
    legacy = fills["legacy"]
    record = ReconciliationRecord(record_id=f"fill:{legacy.activity_id}", reconciliation_type="fill",
                                  subject_id=legacy.activity_id, outcome=ReconciliationOutcome.MATCHED,
                                  expected={"local_fill_id": "old-heuristic"},
                                  actual={"activity_id": legacy.activity_id, "match_method": "heuristic"},
                                  occurred_at=legacy.transaction_time)
    await repositories.reconciliation_records.create_once(record.record_id, record)


def hydrate_asset(symbol: str) -> Any:
    from tradepulse.models import AssetIdentity

    return AssetIdentity(asset_class=AssetClass.EQUITY, symbol=symbol, native_asset_id=f"alpaca:{symbol}")


async def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PersistenceRepositories, SimulatedBroker, dict[str, Any]]:
    clock = ManualClock(T0 - timedelta(hours=1))
    broker, fills = await _history(clock)
    repositories = await _repositories(tmp_path, monkeypatch)
    await _record_locally(repositories, fills)
    return repositories, broker, fills


async def test_bootstrap_complete_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4k."""
    repositories, broker, fills = await _setup(tmp_path, monkeypatch)
    with _lease(tmp_path) as lease:
        record = await bootstrap_fill_cursor(repositories, broker, lease, reason="first Rev.140 deployment", now=DB_CREATED)
    activities = await broker.get_activities(activity_type=None)
    assert record.boundary_activity_id == activities[-1].activity_id == await fill_cursor(repositories)
    assert record.activity_count == len(activities)
    assert record.position_results == {AAPL_KEY: "6", asset_key_from_broker_symbol(AssetClass.EQUITY, "MSFT"): "1"}
    assert record.cash_result["broker_cash"] == record.cash_result["history_cash"]
    outcomes = {row["record_id"]: hydrate("unresolved_execution_outcomes", row["payload"]).outcome
                for row in await paginate_all_rows(repositories.unresolved_execution_outcomes)}
    assert outcomes == {fills["pre"].activity_id: "pre_database", fills["matched"].activity_id: "matched_fill",
                        fills["legacy"].activity_id: "legacy_reconciled"}
    assert [r.activity_id for r in await open_unresolved(repositories)] == [fills["external"].activity_id]
    pages: list[Any] = []
    await broker.get_activities(activity_type=None, page_evidence=pages)
    assert record.page_sha256s == tuple(page["response_hash"] for page in pages)  # the digests reproduce
    await require_bootstrapped(repositories, lease.ref.key)


async def _refused(repositories: PersistenceRepositories, broker: Any, tmp_path: Path, reason: str) -> None:
    tables = ("unresolved_execution_outcomes", "unresolved_broker_executions", "fill_cursor_bootstrap", "broker_activity_cursors")
    with _lease(tmp_path) as lease:
        with pytest.raises(FillHistoryIncomplete, match=reason):
            await bootstrap_fill_cursor(repositories, broker, lease, reason="deploy", now=DB_CREATED)
        for table in tables:
            assert await paginate_all_rows(getattr(repositories, table)) == []
        with pytest.raises(FillHistoryIncomplete, match="FILL_CURSOR_NOT_BOOTSTRAPPED"):
            await require_bootstrapped(repositories, lease.ref.key)


def _filtered(broker: SimulatedBroker, keep: Any) -> Any:
    original = broker.get_activities

    async def get_activities(*args: Any, **kwargs: Any) -> Any:
        return [a for a in await original(*args, **kwargs) if keep(a)]

    broker.get_activities = get_activities  # type: ignore[method-assign]
    return broker


async def test_bootstrap_missing_early_history_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4l: the broker returns history that no longer starts with the first fill."""
    repositories, broker, fills = await _setup(tmp_path, monkeypatch)
    _filtered(broker, lambda a: a.activity_id != fills["pre"].activity_id)
    await _refused(repositories, broker, tmp_path, f"FILL_HISTORY_INCOMPLETE:{AAPL_KEY}")


async def test_bootstrap_truncated_before_funding_fails_inception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4l."""
    repositories, broker, _ = await _setup(tmp_path, monkeypatch)
    _filtered(broker, lambda a: a.activity_type != "JNLC")
    await _refused(repositories, broker, tmp_path, "FILL_HISTORY_INCOMPLETE: inception")


@pytest.mark.parametrize("case", ["unmodelled type", "cash off by a cent", "evidence database", "lease not held", "second bootstrap"])
async def test_bootstrap_refusals_write_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """Spec 4m."""
    repositories, broker, _ = await _setup(tmp_path, monkeypatch)
    if case == "unmodelled type":
        at = T0 + timedelta(days=4, hours=1)
        raw = {"activity_type": "SPLIT", "symbol": "AAPL", "qty": "6", "id": "20261023000000000::split", "date": "2026-10-23"}
        broker._activities.append(_Activity(raw["id"], at, at, raw))
        await _refused(repositories, broker, tmp_path, "FILL_HISTORY_INCOMPLETE: unmodelled SPLIT")
    elif case == "cash off by a cent":
        original = broker.get_account

        async def get_account() -> Any:
            account = await original()
            from dataclasses import replace
            return replace(account, cash=account.cash + Decimal("0.01"))

        broker.get_account = get_account  # type: ignore[method-assign]
        await _refused(repositories, broker, tmp_path, "FILL_HISTORY_INCOMPLETE:cash")
    elif case == "evidence database":
        with sqlite3.connect(tmp_path / "b.db") as connection:
            connection.execute("UPDATE verification_identity SET generation_id='soak-accounting-1' WHERE singleton=1")
        await _refused(repositories, broker, tmp_path, "BOOTSTRAP_REFUSED_EVIDENCE_DATABASE")
    elif case == "lease not held":
        with _lease(tmp_path) as lease:
            pass
        with pytest.raises(FillHistoryIncomplete, match="ACCOUNT_LEASE_NOT_HELD"):
            await bootstrap_fill_cursor(repositories, broker, lease, reason="deploy", now=DB_CREATED)
        assert await fill_cursor(repositories) is None
    else:
        with _lease(tmp_path) as lease:
            first = await bootstrap_fill_cursor(repositories, broker, lease, reason="deploy", now=DB_CREATED)
            with pytest.raises(FillHistoryIncomplete, match="FILL_CURSOR_ALREADY_BOOTSTRAPPED"):
                await bootstrap_fill_cursor(repositories, broker, lease, reason="again", now=DB_CREATED)
        assert [hydrate("fill_cursor_bootstrap", r["payload"]) for r in await paginate_all_rows(repositories.fill_cursor_bootstrap)] == [first]


async def test_legacy_record_cited_not_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repositories, broker, fills = await _setup(tmp_path, monkeypatch)
    legacy_id = f"fill:{fills['legacy'].activity_id}"
    with sqlite3.connect(tmp_path / "b.db") as connection:
        before = connection.execute("SELECT * FROM reconciliation_records WHERE record_id=?", (legacy_id,)).fetchone()
    with _lease(tmp_path) as lease:
        await bootstrap_fill_cursor(repositories, broker, lease, reason="deploy", now=DB_CREATED)
    with sqlite3.connect(tmp_path / "b.db") as connection:
        assert connection.execute("SELECT * FROM reconciliation_records WHERE record_id=?", (legacy_id,)).fetchone() == before
    row = await repositories.unresolved_execution_outcomes.get(fills["legacy"].activity_id)
    assert row is not None and hydrate("unresolved_execution_outcomes", row["payload"]).matched_record_id == legacy_id
