"""Rev.140 task 5: late-arriving activities and P1 completeness (spec §3.1a, tests 4n-4o)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_rev140_bootstrap import AAPL_KEY, _lease, _quote_all, _setup, hydrate_asset
from test_simulated_broker import T0, _order

from tradepulse.alerts import TelegramAlerter
from tradepulse.models import ExecutionMode, Fill, SessionState, Side
from tradepulse.persistence import PersistenceRepositories, paginate_all_rows
from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
from tradepulse.reconciliation.completeness import (
    activity_id_time,
    completeness_pass,
    full_pass_due,
    history_unproven,
)
from tradepulse.reconciliation.unresolved import fill_cursor, open_unresolved, record_pass
from tradepulse.risk import load_session

MONDAY = T0 + timedelta(days=7)  # 2026-10-26, 10:00 New York


def _alerts() -> TelegramAlerter:
    return TelegramAlerter(None, None)


async def _bootstrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PersistenceRepositories, Any]:
    repositories, broker, _ = await _setup(tmp_path, monkeypatch)
    with _lease(tmp_path) as lease:
        await bootstrap_fill_cursor(repositories, broker, lease, reason="deploy", now=broker._clock())
    return repositories, broker


async def _fill(broker: Any, symbol: str, qty: str, coid: str, at: datetime) -> Any:
    broker._clock.set(at)
    _quote_all(broker, at)
    placed = await broker.place_order(_order(symbol, qty, Side.BUY, coid=f"late-{coid}"))
    await broker.get_order(placed.broker_order_id)  # the fill is applied when the simulator next materializes
    return next(a for a in broker._activities if a.raw.get("order_id") == placed.broker_order_id)


async def _pass(repositories: PersistenceRepositories, broker: Any, *, full: bool = False, overlap_days: int = 7) -> Any:
    return await completeness_pass(repositories, broker, _alerts(), now=broker._clock(), full=full, overlap_days=overlap_days)


async def _incremental(repositories: PersistenceRepositories, broker: Any) -> None:
    """Task 4's cursor read: everything listed after C."""
    pages: list[Any] = []
    after = await broker.get_activities(activity_type="FILL", after_id=await fill_cursor(repositories), page_evidence=pages)
    await record_pass(repositories, after, page_proven=True, pass_id="incremental", now=broker._clock())


async def _account_locally(repositories: PersistenceRepositories, activity: Any) -> None:
    raw = activity.raw
    fill = Fill(fill_id=activity.id, trade_intent_id="i-local", order_id=raw["order_id"], asset=hydrate_asset(raw["symbol"]),
                side=Side(raw["side"]), execution_mode=ExecutionMode.PAPER, quantity=Decimal(raw["qty"]),
                price=Decimal(raw["price"]), fees=Decimal(0), slippage=Decimal(0), filled_at=activity.sort_time,
                broker_fill_id=activity.id)
    await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)


def test_activity_id_time_reads_the_new_york_stamp() -> None:
    assert activity_id_time("20261026100000123::abc") == datetime(2026, 10, 26, 14, 0, 0, 123000, tzinfo=UTC)


@pytest.mark.parametrize(("local", "last", "due"), [
    ("2026-10-26T16:05", None, True), ("2026-10-26T16:04", None, False), ("2026-10-24T17:00", None, False),
    ("2026-10-26T17:00", "2026-10-26T16:06", False), ("2026-10-27T16:30", "2026-10-26T16:06", True),
])
def test_full_pass_runs_once_per_trading_day_after_the_close(local: str, last: str | None, due: bool) -> None:
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    now = datetime.fromisoformat(local).replace(tzinfo=ny)
    assert full_pass_due(now, datetime.fromisoformat(last).replace(tzinfo=ny) if last else None) is due


async def test_delayed_activity_found_by_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4n: positions reflect a fill no read lists yet. P1 fails, so the
    asset's sells are refused; a second pass latches. C moves past the fill;
    once it is listed, the overlap read classifies it as a late activity."""
    repositories, broker = await _bootstrapped(tmp_path, monkeypatch)
    hidden = await _fill(broker, "AAPL", "1", "hidden", MONDAY)
    broker.withhold_activity(hidden.id, MONDAY + timedelta(days=2))
    first = await _pass(repositories, broker)
    assert first.p1_failures == (AAPL_KEY,) and await history_unproven(repositories, AAPL_KEY)
    assert (await load_session(repositories)).state != SessionState.FINANCIAL_INTEGRITY_BLOCKED
    await _pass(repositories, broker)
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED and f"FILL_HISTORY_INCOMPLETE:{AAPL_KEY}" in session.financial_integrity_reason
    visible = await _fill(broker, "MSFT", "1", "visible", MONDAY + timedelta(minutes=1))
    await _incremental(repositories, broker)
    assert await fill_cursor(repositories) == visible.id > hidden.id  # C moved past the hidden fill
    broker._clock.set(MONDAY + timedelta(days=2))
    released = await _pass(repositories, broker)
    assert released.late_activity_ids == (hidden.id,) and released.p1_failures == ()
    assert hidden.id in {r.activity_id for r in await open_unresolved(repositories)}
    assert not await history_unproven(repositories, AAPL_KEY)
    notes = [r for r in await paginate_all_rows(repositories.reconciliation_records) if r["record_id"] == f"late_activity:{hidden.id}"]
    assert len(notes) == 1


async def test_delay_beyond_overlap_found_without_waiting_for_the_full_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4n with Δ = 1 day and a 3-day delay. The read since the proven
    baseline lists it as soon as Alpaca does; the daily full pass, run on its
    own, finds it too and proves P1 and cash."""
    repositories, broker = await _bootstrapped(tmp_path, monkeypatch)
    hidden = await _fill(broker, "AAPL", "1", "hidden", MONDAY)
    broker.withhold_activity(hidden.id, MONDAY + timedelta(days=3))
    await _fill(broker, "MSFT", "1", "visible", MONDAY + timedelta(minutes=1))
    await _incremental(repositories, broker)
    broker._clock.set(MONDAY + timedelta(days=3))
    assert (await _pass(repositories, broker, overlap_days=1)).late_activity_ids == (hidden.id,)

    repositories_b, broker_b = await _bootstrapped(tmp_path / "b", monkeypatch)
    hidden_b = await _fill(broker_b, "AAPL", "1", "hidden", MONDAY)
    broker_b.withhold_activity(hidden_b.id, MONDAY + timedelta(days=3))
    await _fill(broker_b, "MSFT", "1", "visible", MONDAY + timedelta(minutes=1))
    await _incremental(repositories_b, broker_b)
    broker_b._clock.set(MONDAY + timedelta(days=3, hours=6, minutes=10))
    full = await _pass(repositories_b, broker_b, full=True, overlap_days=1)
    assert full.late_activity_ids == (hidden_b.id,) and full.p1_failures == ()
    baselines = [r for r in await paginate_all_rows(repositories_b.reconciliation_records)
                 if r["record_id"].startswith("activity_history:baseline:")]
    assert len(baselines) == 1


async def test_same_activity_three_reads_one_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 4o: the incremental, overlap and full reads all return it."""
    repositories, broker = await _bootstrapped(tmp_path, monkeypatch)
    external = await _fill(broker, "AAPL", "1", "external", MONDAY)
    await _incremental(repositories, broker)
    await _pass(repositories, broker)
    await _pass(repositories, broker, full=True)
    rows = [r for r in await paginate_all_rows(repositories.unresolved_broker_executions) if r["record_id"] == external.id]
    assert len(rows) == 1
    assert not [r for r in await paginate_all_rows(repositories.reconciliation_records) if r["record_id"] == f"late_activity:{external.id}"]


async def test_position_lag_one_pass_does_not_latch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repositories, broker = await _bootstrapped(tmp_path, monkeypatch)
    own = await _fill(broker, "AAPL", "1", "own", MONDAY)
    await _account_locally(repositories, own)
    await _incremental(repositories, broker)
    state_before = (await load_session(repositories)).state
    broker.lag_positions(1)
    assert (await _pass(repositories, broker)).p1_failures == (AAPL_KEY,)
    assert await history_unproven(repositories, AAPL_KEY)
    broker.lag_positions(0)
    assert (await _pass(repositories, broker)).p1_failures == ()
    assert not await history_unproven(repositories, AAPL_KEY)
    assert (await load_session(repositories)).state == state_before


async def test_an_unproven_read_makes_every_asset_unproven_without_latching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repositories, broker = await _bootstrapped(tmp_path, monkeypatch)
    original = broker.get_activities

    async def unterminated(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if kwargs.get("page_evidence") is not None:
            kwargs["page_evidence"][-1]["terminal"] = False
        return result

    broker.get_activities = unterminated
    state_before = (await load_session(repositories)).state
    assert (await _pass(repositories, broker)).p1_failures == ("*",)
    assert await history_unproven(repositories, AAPL_KEY) and await history_unproven(repositories, "crypto:default:alpaca:BTCUSD")
    assert (await _pass(repositories, broker)).p1_failures == ("*",)
    assert (await load_session(repositories)).state == state_before
    broker.get_activities = original
    assert (await _pass(repositories, broker)).p1_failures == ()
    assert not await history_unproven(repositories, AAPL_KEY)
