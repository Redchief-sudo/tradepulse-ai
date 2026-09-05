from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.execution.fill_attribution import attribute_order_fills, terminal_status_for_order
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Fill, Side, TradeIntent, TradeIntentStatus
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories


@pytest.mark.parametrize(
    ("order_status", "attributed_qty", "requested_quantity", "expected"),
    [
        # done_for_day is not a permanent disposition -- Alpaca may still
        # send further updates the next trading day, so a genuine partial
        # maps to the same non-terminal-in-this-system PARTIALLY_FILLED a
        # live partial fill would get, and a zero-fill day is inconclusive
        # (None) rather than any kind of false-cancellation.
        ("done_for_day", Decimal("4"), Decimal("10"), TradeIntentStatus.PARTIALLY_FILLED),
        ("done_for_day", Decimal("10"), Decimal("10"), TradeIntentStatus.FILLED),
        ("done_for_day", Decimal("0"), Decimal("10"), None),
        # Broker failure statuses map to their OWN distinct TradeIntentStatus
        # when nothing was attributed, not a single generic REJECTED.
        ("canceled", Decimal("4"), Decimal("10"), TradeIntentStatus.PARTIALLY_FILLED),
        ("canceled", Decimal("0"), Decimal("10"), TradeIntentStatus.CANCELED),
        ("expired", Decimal("0"), Decimal("10"), TradeIntentStatus.EXPIRED),
        ("rejected", Decimal("0"), Decimal("10"), TradeIntentStatus.REJECTED),
        ("replaced", Decimal("0"), Decimal("10"), TradeIntentStatus.CANCELED),
        # `filled` always means the order's own full submitted quantity was
        # executed -- anything less is a broker-side contradiction, not
        # evidence of a real partial fill, and must never be finalized.
        ("filled", Decimal("10"), Decimal("10"), TradeIntentStatus.FILLED),
        ("filled", Decimal("0"), Decimal("10"), None),
        ("filled", Decimal("4"), Decimal("10"), None),
    ],
)
def test_terminal_status_for_order_is_quantity_aware(order_status, attributed_qty, requested_quantity, expected) -> None:
    assert terminal_status_for_order(order_status, attributed_qty, requested_quantity) == expected


def test_terminal_status_for_order_treats_missing_requested_quantity_as_unresolvable_filled() -> None:
    """No requested_quantity to compare against -- can't confirm full
    attribution, so `filled` must not be forced through; the caller is
    expected to leave the intent non-terminal rather than guess."""
    assert terminal_status_for_order("filled", Decimal("10"), None) is None


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _broker() -> AlpacaClient:
    return AlpacaClient("key", "secret", "paper", 10)


def _alerts() -> TelegramAlerter:
    return TelegramAlerter(None, None)


def _intent() -> TradeIntent:
    return TradeIntent(
        "ti-1", "ti-1", "ti-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("10"), broker_order_id="order-1", client_order_id="ti-1",
    )


def _mock_activities(activities: list[dict]) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=activities))


def _activity_json(activity_id: str, qty: str, price: str, order_id: str = "order-1") -> dict:
    return {
        "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
        "qty": qty, "price": price, "transaction_time": NOW.isoformat().replace("+00:00", "Z"), "order_id": order_id,
    }


@respx.mock
async def test_a_fill_persisted_without_its_settlement_event_is_repaired_exactly_once_on_replay(tmp_path) -> None:
    """FIN-091-01 regression: simulates the crash window between the two
    separate transactions in attribute_order_fills -- a Fill already exists
    for this broker activity, but the SettlementEvent write that was
    supposed to follow it never happened. Replaying the same broker
    activity must repair the missing SettlementEvent, not skip it forever
    just because fills.create_once now returns False for the already-
    persisted fill_id."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    intent = _intent()

    orphaned_fill = Fill(
        fill_id="act-1", trade_intent_id=intent.trade_intent_id, order_id=intent.broker_order_id,
        asset=_aapl(), side=Side.BUY, execution_mode=ExecutionMode.PAPER,
        quantity=Decimal("10"), price=Decimal("150"), fees=Decimal("0"), slippage=Decimal("0"),
        filled_at=NOW, broker_fill_id="act-1",
    )
    await repositories.fills.create_once(orphaned_fill.fill_id, orphaned_fill, unique_value=orphaned_fill.fill_id)
    assert await repositories.settlements.get("act-1") is None

    _mock_activities([_activity_json("act-1", "10", "150")])
    attributed = await attribute_order_fills(repositories, broker, _alerts(), intent, clock=lambda: NOW)
    await broker.aclose()

    assert attributed.quantity == Decimal("10")
    settlement_event = await repositories.settlements.get("act-1")
    assert settlement_event is not None

    fills = await repositories.fills.list_all()
    assert len(fills) == 1


@respx.mock
async def test_replaying_an_already_fully_settled_fill_does_not_duplicate_or_disturb_it(tmp_path) -> None:
    """The unconditional settlements.create_once call must be a genuine
    no-op once a SettlementEvent already exists -- replaying the same
    broker activity again (e.g. a second late-fill recovery pass) must not
    create a second Fill or SettlementEvent, nor overwrite the existing
    settlement's recorded state."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    intent = _intent()

    _mock_activities([_activity_json("act-1", "10", "150")])
    first = await attribute_order_fills(repositories, broker, _alerts(), intent, clock=lambda: NOW)
    assert first.quantity == Decimal("10")
    assert await repositories.settlements.get("act-1") is not None

    second = await attribute_order_fills(repositories, broker, _alerts(), intent, clock=lambda: NOW)
    await broker.aclose()

    assert second.quantity == Decimal("10")
    assert len(await repositories.fills.list_all()) == 1
    assert len(await repositories.settlements.list_all()) == 1
