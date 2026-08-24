from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    Holding,
    PositionLot,
    ReconciliationOutcome,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.reconciliation import run_reconciliation
from tradepulse.settlement import SettlementProcessor

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


def _settlement(repositories: PersistenceRepositories) -> SettlementProcessor:
    return SettlementProcessor(repositories, _alerts(), clock=lambda: NOW)


def _mock_positions(positions: list[dict]) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=positions))


def _mock_activities(activities: list[dict]) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=activities))


async def _seed_lot(repositories: PersistenceRepositories, *, lot_id: str, fill_id: str, quantity: str, price: str, opened_at: datetime = NOW) -> None:
    lot = PositionLot(
        lot_id=lot_id, originating_fill_id=fill_id, asset=_aapl(), position_side="long",
        opened_quantity=Decimal(quantity), remaining_quantity=Decimal(quantity), acquisition_price=Decimal(price),
        opened_at=opened_at,
    )
    await repositories.position_lots.create_once(lot_id, lot, unique_value=fill_id)


async def _seed_holding(repositories: PersistenceRepositories, *, quantity: str, average_price: str = "150") -> None:
    holding = Holding(asset=_aapl(), quantity=Decimal(quantity), average_price=Decimal(average_price), updated_at=NOW)
    await repositories.holdings.create_once("AAPL", holding)


async def _seed_fill(
    repositories: PersistenceRepositories, *, fill_id: str, quantity: str, price: str,
    filled_at: datetime = NOW, side: Side = Side.BUY, broker_fill_id: str | None = None,
) -> None:
    fill = Fill(
        fill_id, "ti-1", "order-1", _aapl(), side, ExecutionMode.PAPER, Decimal(quantity), Decimal(price),
        Decimal("0"), Decimal("0"), filled_at, broker_fill_id=broker_fill_id,
    )
    await repositories.fills.create_once(fill_id, fill, unique_value=None)


async def _seed_intent(
    repositories: PersistenceRepositories, *, trade_intent_id: str, broker_order_id: str,
    status: TradeIntentStatus = TradeIntentStatus.ACCEPTED, side: Side = Side.BUY,
) -> None:
    intent = TradeIntent(
        trade_intent_id, trade_intent_id, trade_intent_id, _aapl(), side, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("5"), status=status, broker_order_id=broker_order_id, client_order_id=trade_intent_id,
    )
    await repositories.trade_intents.create_once(trade_intent_id, intent, status=intent.status.value, unique_value=trade_intent_id)


def _position_json(qty: str, current_price: str = "150") -> dict:
    return {
        "symbol": "AAPL", "asset_class": "us_equity", "qty": qty, "avg_entry_price": "150",
        "market_value": "0", "current_price": current_price, "unrealized_pl": "0",
    }


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None, order_id: str = "order-1", side: str = "buy") -> dict:
    return {
        "id": order_id, "status": status, "symbol": "AAPL", "side": side,
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": NOW.isoformat().replace("+00:00", "Z"),
    }


def _mock_order(order_id: str, status: str, filled_qty: str, filled_avg_price: str | None, side: str = "buy") -> None:
    respx.get(f"https://paper-api.alpaca.markets/v2/orders/{order_id}").mock(
        return_value=httpx.Response(200, json=_order_json(status, filled_qty, filled_avg_price, order_id=order_id, side=side))
    )


def _activity_json(
    activity_id: str, qty: str, price: str, transaction_time: datetime = NOW, side: str = "buy", order_id: str | None = None,
) -> dict:
    activity = {
        "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": side,
        "qty": qty, "price": price, "transaction_time": transaction_time.isoformat().replace("+00:00", "Z"),
    }
    if order_id is not None:
        activity["order_id"] = order_id
    return activity


@respx.mock
async def test_all_three_agree_records_matched_only(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_lot(repositories, lot_id="lot-1", fill_id="fill-1", quantity="10", price="150")
    await _seed_holding(repositories, quantity="10")
    _mock_positions([_position_json("10")])
    _mock_activities([])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "ok"
    assert summary.positions_checked == 1
    assert summary.view_drift_corrected == 0
    assert summary.accounting_drift_detected == 0

    records = await repositories.reconciliation_records.list_all()
    outcomes = [hydrate("reconciliation_records", r["payload"]).outcome for r in records]
    assert outcomes == [ReconciliationOutcome.MATCHED]


@respx.mock
async def test_stale_holding_view_is_rebuilt_when_lots_agree_with_broker(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_lot(repositories, lot_id="lot-1", fill_id="fill-1", quantity="10", price="150")
    await _seed_holding(repositories, quantity="5")  # stale -- lots/broker both say 10
    _mock_positions([_position_json("10")])
    _mock_activities([])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.view_drift_corrected == 1
    assert summary.accounting_drift_detected == 0

    holding_row = await repositories.holdings.get("AAPL")
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("10")  # rebuilt to match lots/broker

    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    types_outcomes = {(p.reconciliation_type, p.outcome) for p in payloads}
    assert ("position_view", ReconciliationOutcome.DRIFT_DETECTED) in types_outcomes
    assert ("position_view", ReconciliationOutcome.CORRECTED) in types_outcomes


@respx.mock
async def test_lots_disagreeing_with_broker_is_accounting_drift_not_corrected(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_lot(repositories, lot_id="lot-1", fill_id="fill-1", quantity="5", price="150")  # lots say 5
    await _seed_holding(repositories, quantity="5")
    _mock_positions([_position_json("10")])  # broker says 10 -- a missed fill somewhere
    _mock_activities([])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.accounting_drift_detected == 1
    assert summary.view_drift_corrected == 0

    holding_row = await repositories.holdings.get("AAPL")
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("5")  # untouched -- NOT silently corrected

    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    assert len(payloads) == 1
    assert payloads[0].reconciliation_type == "position_accounting"
    assert payloads[0].outcome == ReconciliationOutcome.DRIFT_DETECTED
    # no CORRECTED record was ever written for this symbol
    assert not any(p.outcome == ReconciliationOutcome.CORRECTED for p in payloads)


@respx.mock
async def test_local_holding_for_a_closed_position_is_deleted_when_lots_agree_its_gone(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_holding(repositories, quantity="10")  # stale holding, no open lots, no broker position
    _mock_positions([])
    _mock_activities([])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.view_drift_corrected == 1
    assert await repositories.holdings.get("AAPL") is None


@respx.mock
async def test_missed_fill_is_drift_detected_and_never_fabricated(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    _mock_positions([])
    _mock_activities([_activity_json("activity-999", "5", "150")])  # no local fill matches this at all

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.fills_checked == 1
    assert summary.missed_fills_detected == 1
    assert (await repositories.fills.list_all()) == []  # never fabricated

    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.DRIFT_DETECTED
    assert fill_records[0].actual["activity_id"] == "activity-999"


@respx.mock
async def test_matched_fill_is_recorded_as_heuristic_match(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_fill(repositories, fill_id="fill-1", quantity="5", price="150")
    _mock_positions([])
    _mock_activities([_activity_json("activity-1", "5", "150")])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 0
    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.MATCHED
    assert fill_records[0].actual["match_method"] == "heuristic"


@respx.mock
async def test_exact_activity_id_match_wins_even_when_heuristic_would_not_match(tmp_path) -> None:
    """Once a local Fill carries Alpaca's real broker_fill_id (see
    execution/gateway.py::_attribute_order_fills), reconciliation should
    use it directly rather than falling back to the symbol/qty/price/time
    heuristic -- proven here by seeding a Fill whose qty/price would NOT
    satisfy the heuristic at all, yet still gets matched via the exact ID."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_fill(repositories, fill_id="fill-1", quantity="5", price="150", broker_fill_id="activity-1")
    _mock_positions([])
    # Heuristic-incompatible on purpose: different qty/price than the seeded Fill.
    _mock_activities([_activity_json("activity-1", "999", "1")])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 0
    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.MATCHED
    assert fill_records[0].expected["local_fill_id"] == "fill-1"
    assert fill_records[0].actual["match_method"] == "exact_id"


@respx.mock
async def test_heuristic_matching_does_not_cross_match_opposite_sides(tmp_path) -> None:
    """A same-symbol, same-quantity, same-price BUY and SELL in the same
    window (a realistic scan-then-flip) must never be matched to the wrong
    local Fill just because qty/price/time happen to coincide."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_fill(repositories, fill_id="fill-buy", quantity="5", price="150", side=Side.BUY)
    await _seed_fill(repositories, fill_id="fill-sell", quantity="5", price="150", side=Side.SELL)
    _mock_positions([])
    _mock_activities([_activity_json("activity-buy", "5", "150", side="buy")])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 0
    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.MATCHED
    assert fill_records[0].expected["local_fill_id"] == "fill-buy"  # never the sell fill


@respx.mock
async def test_late_fill_recovered_into_non_terminal_intent(tmp_path) -> None:
    """The gateway's live poll window already expired on this order (it's
    still ACCEPTED locally), but Alpaca shows it genuinely filled. Since the
    activity's order_id ties it to a known local TradeIntent, reconciliation
    should recover it through the same accounting path the gateway uses --
    not fabricate anything, not leave it stranded."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_intent(repositories, trade_intent_id="ti-1", broker_order_id="order-1", status=TradeIntentStatus.ACCEPTED)
    _mock_positions([])
    _mock_activities([_activity_json("activity-1", "5", "150", order_id="order-1")])
    _mock_order("order-1", "filled", "5", "150")

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 0
    assert summary.late_fills_recovered == 1

    fill_row = await repositories.fills.get("activity-1")
    assert fill_row is not None  # keyed by the real Alpaca activity ID, never a synthesized one

    intent_row = await repositories.trade_intents.get("ti-1")
    assert intent_row["status"] == "filled"  # finalized, not left stranded at ACCEPTED

    holding_row = await repositories.holdings.get("AAPL")
    assert holding_row is not None  # proves SettlementProcessor.process_pending() actually ran
    assert hydrate("holdings", holding_row["payload"]).quantity == Decimal("5")

    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.CORRECTED


@respx.mock
async def test_late_fill_recovered_into_already_terminal_intent(tmp_path) -> None:
    """An intent that already reached FILLED can still be missing one of its
    real fill activities (e.g. one attributed slice never made it into a
    Fill before the poll window closed). Recovery must still create the
    missing Fill/SettlementEvent -- and must never need to re-check the
    order's broker status to do it, proven here by never mocking get_order
    at all (an unmocked call would fail the test)."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_intent(repositories, trade_intent_id="ti-2", broker_order_id="order-2", status=TradeIntentStatus.FILLED)
    _mock_positions([])
    _mock_activities([_activity_json("activity-2", "5", "150", order_id="order-2")])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.late_fills_recovered == 1
    fill_row = await repositories.fills.get("activity-2")
    assert fill_row is not None

    intent_row = await repositories.trade_intents.get("ti-2")
    assert intent_row["status"] == "filled"  # untouched -- was already terminal


@respx.mock
async def test_orphaned_activity_with_no_matching_intent_still_never_fabricated(tmp_path) -> None:
    """An activity whose order_id matches no local TradeIntent at all must
    fall straight through to the unchanged missed-fill path -- recovery is
    strictly additive and never loosens the no-fabrication guarantee."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    _mock_positions([])
    _mock_activities([_activity_json("activity-orphan", "5", "150", order_id="order-does-not-exist-locally")])

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 1
    assert summary.late_fills_recovered == 0
    assert (await repositories.fills.list_all()) == []

    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.DRIFT_DETECTED


@respx.mock
async def test_late_fill_recovery_attempted_but_activity_still_invalid_falls_through_to_missed(tmp_path) -> None:
    """A known order matches by order_id, but the activity itself fails the
    same validation attribute_order_fills always applies (here: wrong side)
    -- a genuine anomaly, not a lag. Must still surface as a missed fill,
    not a silent no-op."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_intent(repositories, trade_intent_id="ti-3", broker_order_id="order-3", status=TradeIntentStatus.ACCEPTED, side=Side.BUY)
    _mock_positions([])
    _mock_activities([_activity_json("activity-3", "5", "150", side="sell", order_id="order-3")])  # wrong side vs the BUY intent
    _mock_order("order-3", "accepted", "0", None)

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 1
    assert summary.late_fills_recovered == 0
    assert (await repositories.fills.list_all()) == []

    intent_row = await repositories.trade_intents.get("ti-3")
    assert intent_row["status"] == "accepted"  # untouched


@respx.mock
async def test_late_fill_recovery_maps_done_for_day_partial_to_partially_filled(tmp_path) -> None:
    """Recovery shares terminal_status_for_order with the live gateway path
    -- a done_for_day order that only partially filled must land on
    PARTIALLY_FILLED here too, never be folded into FILLED just because
    it's a non-failure terminal order status."""
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_intent(repositories, trade_intent_id="ti-4", broker_order_id="order-4", status=TradeIntentStatus.ACCEPTED)
    _mock_positions([])
    _mock_activities([_activity_json("activity-4", "3", "150", order_id="order-4")])  # requested 5, only 3 filled
    _mock_order("order-4", "done_for_day", "3", "150")

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.late_fills_recovered == 1
    intent_row = await repositories.trade_intents.get("ti-4")
    assert intent_row["status"] == "partially_filled"


@respx.mock
async def test_broker_positions_outage_reports_degraded(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(side_effect=httpx.ConnectError("connection refused"))

    summary = await run_reconciliation(repositories, broker, _settlement(repositories), _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "degraded"
    assert summary.error is not None
