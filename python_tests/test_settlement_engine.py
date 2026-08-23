from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.alerts import TelegramAlerter
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    PositionLot,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.settlement import SettlementProcessor


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def asset() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _no_op_alerter() -> TelegramAlerter:
    return TelegramAlerter(bot_token=None, chat_id=None)  # no creds -- send() no-ops, never raises


async def _seed_buy(repositories: PersistenceRepositories, *, fill_id: str = "fill-1", quantity: str = "10", price: str = "150") -> None:
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal(quantity),
    )
    await repositories.trade_intents.create_once("ti-1", intent, status=intent.status.value, unique_value=intent.idempotency_key)

    fill = Fill(fill_id, "ti-1", "order-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once(fill_id, fill, unique_value=None)

    event = SettlementEvent("se-1", fill_id, "ti-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), NOW)
    await repositories.settlements.create_once("se-1", event, status=event.status.value, unique_value=fill_id)


async def test_buy_settlement_projects_lot_holding_and_intent_summary(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    summary = await processor.process_pending()

    assert summary.processed == 1
    assert summary.completed == 1
    assert summary.ok

    lot_rows = await repositories.position_lots.list_all()
    assert len(lot_rows) == 1
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.position_side == "long"
    assert lot.remaining_quantity == Decimal("10")

    holding_row = await repositories.holdings.get("AAPL")
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("10")
    assert holding.average_price == Decimal("150")

    intent_row = await repositories.trade_intents.get("ti-1")
    intent = hydrate("trade_intents", intent_row["payload"])
    assert intent.filled_quantity == Decimal("10")
    assert intent.filled_avg_price == Decimal("150")

    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.COMPLETED.value
    event = hydrate("settlements", event_row["payload"])
    assert event.integrity_verified


async def test_replaying_a_completed_event_is_a_no_op(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    await processor.process_pending()
    second_summary = await processor.process_pending()

    assert second_summary.processed == 0  # COMPLETED events are not processable again
    lot_rows = await repositories.position_lots.list_all()
    assert len(lot_rows) == 1  # no duplicate lot created by the replay


async def test_sell_closes_the_long_lot_and_realizes_pnl(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    sell_intent = TradeIntent(
        "ti-2", "idem-2", "corr-2", asset(), Side.SELL, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"),
    )
    await repositories.trade_intents.create_once("ti-2", sell_intent, status=sell_intent.status.value, unique_value=sell_intent.idempotency_key)
    sell_fill = Fill("fill-2", "ti-2", "order-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-2", sell_fill, unique_value=None)
    sell_event = SettlementEvent("se-2", "fill-2", "ti-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), NOW)
    await repositories.settlements.create_once("se-2", sell_event, status=sell_event.status.value, unique_value="fill-2")

    summary = await processor.process_pending()
    assert summary.completed == 1

    holding_row = await repositories.holdings.get("AAPL")
    assert holding_row is None  # fully closed position -- the Holding row must be gone, not zero-quantity

    lot_rows = await repositories.position_lots.list_all()
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.status == "closed"
    assert lot.realized_pnl == Decimal("150")  # (165-150)*10


async def test_manufactured_integrity_violation_blocks_permanently_and_alerts(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    # A lot that already claims to have closed more than the incoming event's
    # own quantity for the same fill_id -- an impossible state that only a
    # data-corruption bug could produce, exactly what verify/lot-planning
    # must catch.
    corrupted_lot = PositionLot(
        "lot-x", "origin-x", asset(), "long", Decimal("10"), Decimal("10"), Decimal("100"), NOW,
        closures={"fill-bad": Decimal("999")},
    )
    await repositories.position_lots.create_once("lot-x", corrupted_lot, unique_value="origin-x")

    intent = TradeIntent("ti-3", "idem-3", "corr-3", asset(), Side.SELL, ExecutionMode.PAPER, "manual", NOW, requested_quantity=Decimal("5"))
    await repositories.trade_intents.create_once("ti-3", intent, status=intent.status.value, unique_value=intent.idempotency_key)
    bad_event = SettlementEvent("se-3", "fill-bad", "ti-3", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("5"), Decimal("100"), NOW)
    await repositories.settlements.create_once("se-3", bad_event, status=bad_event.status.value, unique_value="fill-bad")

    alerts = _no_op_alerter()
    processor = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    summary = await processor.process_pending()

    assert summary.integrity_blocked == 1
    assert not summary.ok

    event_row = await repositories.settlements.get("se-3")
    assert event_row["status"] == SettlementStatus.INTEGRITY_BLOCKED.value
    event = hydrate("settlements", event_row["payload"])
    assert event.next_retry_at is None  # permanent -- never auto-retried
    assert event.error_code is not None and "OVERALLOCATED" in event.error_code

    # A second run must not retry an integrity_blocked event either.
    second_summary = await processor.process_pending()
    assert second_summary.processed == 0
