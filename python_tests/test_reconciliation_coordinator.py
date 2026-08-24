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
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.reconciliation import run_reconciliation

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


def _position_json(qty: str, current_price: str = "150") -> dict:
    return {
        "symbol": "AAPL", "asset_class": "us_equity", "qty": qty, "avg_entry_price": "150",
        "market_value": "0", "current_price": current_price, "unrealized_pl": "0",
    }


def _activity_json(activity_id: str, qty: str, price: str, transaction_time: datetime = NOW, side: str = "buy") -> dict:
    return {
        "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": side,
        "qty": qty, "price": price, "transaction_time": transaction_time.isoformat().replace("+00:00", "Z"),
    }


@respx.mock
async def test_all_three_agree_records_matched_only(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    await _seed_lot(repositories, lot_id="lot-1", fill_id="fill-1", quantity="10", price="150")
    await _seed_holding(repositories, quantity="10")
    _mock_positions([_position_json("10")])
    _mock_activities([])

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.view_drift_corrected == 1
    assert await repositories.holdings.get("AAPL") is None


@respx.mock
async def test_missed_fill_is_drift_detected_and_never_fabricated(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    _mock_positions([])
    _mock_activities([_activity_json("activity-999", "5", "150")])  # no local fill matches this at all

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
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

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.missed_fills_detected == 0
    records = await repositories.reconciliation_records.list_all()
    payloads = [hydrate("reconciliation_records", r["payload"]) for r in records]
    fill_records = [p for p in payloads if p.reconciliation_type == "fill"]
    assert len(fill_records) == 1
    assert fill_records[0].outcome == ReconciliationOutcome.MATCHED
    assert fill_records[0].expected["local_fill_id"] == "fill-buy"  # never the sell fill


@respx.mock
async def test_broker_positions_outage_reports_degraded(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    broker = _broker()
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(side_effect=httpx.ConnectError("connection refused"))

    summary = await run_reconciliation(repositories, broker, _alerts(), clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "degraded"
    assert summary.error is not None
