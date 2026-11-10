"""Rev.154 on every conformant broker: the all-status order history that
resolve-dispatch relies on, and its refusals against a real broker order.

Run on the simulators by default; on the Alpaca paper account with
``-m integration``. ``test_history_*`` is read-only. ``test_order_*`` places
one 1-share SPY limit buy at $1.00 (far below the market, so it rests),
cancels it, and needs the order guard (``TRADEPULSE_CONFORMANCE_ORDERS=1``,
the account lease, no runtime, an open market).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tradepulse.broker.port import OrderRequest, account_ref
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Side, TradeIntent, TradeIntentStatus
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.reconciliation.dispatch import DispatchResolutionRefused, resolve_dispatch

from .conftest import OrderLedger, Venue

HISTORY_DAYS = 120  # the paper account has real orders in this window (73 on 2026-11-09)
SPY = AssetIdentity("SPY", AssetClass.EQUITY, "alpaca:SPY")


def _now(venue: Venue) -> datetime:
    return venue.clock() if venue.clock is not None else datetime.now(UTC)


async def _pause(venue: Venue) -> None:
    if venue.clock is not None:
        venue.clock.advance(timedelta(seconds=1))
    else:
        await asyncio.sleep(1)


def _check_proof(orders, pages, since: datetime) -> None:
    assert pages, "no page evidence"
    assert pages[-1]["terminal"] is True and not any(page["terminal"] for page in pages[:-1])
    assert all(page["request"].get("status") == "all" for page in pages)
    ids = [order.broker_order_id for order in orders]
    assert len(ids) == len(set(ids)), "an order repeated across pages"
    assert set(ids) == {order_id for page in pages for order_id in page["order_ids"]}
    for order in orders:
        assert order.submitted_at is not None and order.submitted_at >= since - timedelta(microseconds=1)


async def test_history_of_all_statuses_is_complete_with_page_proof(conformant_broker: Venue) -> None:
    venue, broker = conformant_broker, conformant_broker.broker
    if venue.sim is not None:
        # A history (the venue already quotes SPY): one order filled, one resting, one cancelled.
        venue.sim.schedule_fills("hist-filled", [(timedelta(seconds=1), Decimal(1), None)])
        await broker.place_order(OrderRequest(symbol="SPY", qty=Decimal(1), side=Side.BUY, client_order_id="hist-filled"))
        resting = await broker.place_order(OrderRequest(symbol="SPY", qty=Decimal(1), side=Side.BUY, order_type="limit",
                                                        limit_price=Decimal("1.00"), client_order_id="hist-resting"))
        cancelled = await broker.place_order(OrderRequest(symbol="SPY", qty=Decimal(1), side=Side.BUY, order_type="limit",
                                                          limit_price=Decimal("1.00"), client_order_id="hist-cancelled"))
        await broker.cancel_order(cancelled.broker_order_id)
        venue.clock.advance(timedelta(seconds=5))
        assert resting.broker_order_id
    since = _now(venue) - timedelta(days=HISTORY_DAYS)
    pages: list[dict] = []
    orders = await broker.get_order_history(since, page_evidence=pages)
    _check_proof(orders, pages, since)
    history = {order.broker_order_id for order in orders}
    # Every currently open order placed in the window is in the history.
    open_in_window = {order.broker_order_id for order in await broker.get_open_orders()
                      if order.submitted_at is not None and order.submitted_at >= since}
    assert open_in_window <= history
    if venue.sim is not None:
        statuses = {order.raw["client_order_id"]: order.status for order in orders}
        assert statuses["hist-filled"] == "filled" and statuses["hist-cancelled"] == "canceled"
        assert statuses["hist-resting"] not in ("filled", "canceled", "expired", "rejected")


async def _ambiguous_intent(repositories: PersistenceRepositories, intent_id: str, account: str,
                            dispatched_at: datetime) -> None:
    intent = TradeIntent(intent_id, f"idem-{intent_id}", f"corr-{intent_id}", SPY, Side.BUY, ExecutionMode.PAPER,
                         "conformance", dispatched_at - timedelta(seconds=1), requested_quantity=Decimal(1),
                         status=TradeIntentStatus.SUBMITTED, risk_snapshot={"broker_account_number": account},
                         dispatched_at=dispatched_at)
    await repositories.trade_intents.create_once(intent_id, intent, status=intent.status.value,
                                                 unique_value=intent.idempotency_key)


async def test_order_a_real_order_blocks_dispatch_resolution(conformant_broker: Venue, order_ledger: OrderLedger,
                                                            order_guard: object, tmp_path: Path) -> None:
    """The approved paper order: 1 SPY limit buy at $1.00, cancelled at once.

    - Its client order id is found, and the history lists it as cancelled.
    - resolve-dispatch refuses to clear the intent that owns it
      (BROKER_ORDER_PRESENT), and refuses to clear another, absent SPY
      dispatch while that order is unexplained in the window
      (UNEXPLAINED_ASSET_ORDER). No local state changes in either case."""
    venue, broker = conformant_broker, conformant_broker.broker
    client_order_id = f"conformance-dispatch-{uuid4()}"
    placed = await order_ledger.place(OrderRequest(symbol="SPY", qty=Decimal(1), side=Side.BUY, order_type="limit",
                                                   time_in_force="day", limit_price=Decimal("1.00"),
                                                   client_order_id=client_order_id))
    assert placed.broker_order_id and placed.raw["client_order_id"] == client_order_id
    await broker.cancel_order(placed.broker_order_id)
    for _ in range(30):
        final = await broker.get_order(placed.broker_order_id)
        if final.status in ("canceled", "expired", "rejected", "filled"):
            break
        await _pause(venue)
    assert (final.status, final.filled_qty) == ("canceled", Decimal(0))
    found = await broker.get_order_by_client_order_id(client_order_id)
    assert found is not None and found.broker_order_id == placed.broker_order_id

    now = _now(venue)
    pages: list[dict] = []
    since = now - timedelta(minutes=30)
    history = await broker.get_order_history(since, page_evidence=pages)
    _check_proof(history, pages, since)
    [listed] = [order for order in history if order.broker_order_id == placed.broker_order_id]
    assert listed.status == "canceled" and listed.raw["client_order_id"] == client_order_id

    ref = await account_ref(broker)
    lease = order_guard if order_guard is not None else SimpleNamespace(verify=lambda: None, ref=ref)
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'dispatch.db'}")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    dispatched_at = now - timedelta(minutes=20)
    absent_id = f"conformance-absent-{uuid4()}"
    await _ambiguous_intent(repositories, client_order_id, ref.account_number, dispatched_at)
    await _ambiguous_intent(repositories, absent_id, ref.account_number, dispatched_at)
    with pytest.raises(DispatchResolutionRefused, match="BROKER_ORDER_PRESENT"):
        await resolve_dispatch(repositories, broker, client_order_id, "conformance: the broker holds this order",
                               account_lease=lease, clock=lambda: _now(venue))
    with pytest.raises(DispatchResolutionRefused, match="UNEXPLAINED_ASSET_ORDER"):
        await resolve_dispatch(repositories, broker, absent_id, "conformance: another SPY order is in the window",
                               account_lease=lease, clock=lambda: _now(venue))
    for intent_id in (client_order_id, absent_id):
        assert (await repositories.trade_intents.get(intent_id))["status"] == "submitted"
    assert await database.run(lambda c: c.execute("SELECT COUNT(*) FROM resolved_dispatch_watches").fetchone()[0]) == 0
