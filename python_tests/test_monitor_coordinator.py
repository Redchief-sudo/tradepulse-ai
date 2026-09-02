import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import respx

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Holding,
    PositionLot,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
)
from tradepulse.monitor import run_position_monitor
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.settlement import SettlementProcessor


NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
QUOTE_TS = NOW.isoformat().replace("+00:00", "Z")
LIMITS = risk_limits_for_profile("balanced")


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _setup(tmp_path):
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    broker = AlpacaClient("key", "secret", "paper", 10)
    market_data = AlpacaMarketDataProvider(broker)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    limits = risk_limits_for_profile("balanced")
    gateway = ExecutionGateway(repositories, broker, market_data, settlement, alerts, limits, ExecutionMode.PAPER, clock=lambda: NOW)
    return repositories, broker, gateway, alerts


def _positions_json(qty: str, current_price: str, avg_entry: str = "150") -> list[dict]:
    return [
        {
            "symbol": "AAPL", "asset_class": "us_equity", "qty": qty, "avg_entry_price": avg_entry,
            "market_value": "0", "current_price": current_price, "unrealized_pl": "0",
        }
    ]


def _mock_positions(qty: str, current_price: str) -> None:
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(
        return_value=httpx.Response(200, json=_positions_json(qty, current_price))
    )


def _mock_account(cash: str = "50000", equity: str = "100000") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": equity, "last_equity": "99500", "cash": cash, "buying_power": equity, "portfolio_value": equity})
    )


def _mock_quote(bid: str = "134.50", ask: str = "135.50") -> None:
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": float(bid), "ap": float(ask), "t": QUOTE_TS}})
    )


def _order_json(status: str, filled_qty: str, filled_avg_price: str | None, side: str = "sell") -> dict:
    return {
        "id": "order-1", "status": status, "symbol": "AAPL", "side": side,
        "filled_qty": filled_qty, "filled_avg_price": filled_avg_price, "submitted_at": QUOTE_TS,
    }


def _mock_fill_activities(activity_id: str, qty: str, price: str, side: str = "sell", order_id: str = "order-1") -> None:
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": activity_id, "activity_type": "FILL", "symbol": "AAPL", "side": side,
                "qty": qty, "price": price, "transaction_time": QUOTE_TS, "order_id": order_id,
            }],
        )
    )


async def _seed_holding(repositories: PersistenceRepositories, *, quantity: str = "10", stop_loss: str | None = "140", target_price: str | None = "170") -> None:
    holding = Holding(
        asset=_aapl(), quantity=Decimal(quantity), average_price=Decimal("150"), updated_at=NOW,
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        target_price=Decimal(target_price) if target_price else None,
    )
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)


async def _seed_lot(
    repositories: PersistenceRepositories, *, lot_id: str = "lot-1", quantity: str = "10",
    acquisition_price: str = "150", position_side: str = "long", opened_at: datetime = NOW,
    mfe_price: str | None = None, mae_price: str | None = None, remaining_quantity: str | None = None,
) -> None:
    lot = PositionLot(
        lot_id=lot_id, originating_fill_id=f"fill-{lot_id}", asset=_aapl(), position_side=position_side,
        opened_quantity=Decimal(quantity),
        remaining_quantity=Decimal(remaining_quantity) if remaining_quantity is not None else Decimal(quantity),
        acquisition_price=Decimal(acquisition_price), opened_at=opened_at,
        mfe_price=Decimal(mfe_price) if mfe_price is not None else None,
        mae_price=Decimal(mae_price) if mae_price is not None else None,
    )
    await repositories.position_lots.create_once(lot_id, lot, unique_value=lot.originating_fill_id)


async def _get_lot(repositories: PersistenceRepositories, lot_id: str) -> PositionLot:
    row = await repositories.position_lots.get(lot_id)
    return hydrate("position_lots", row["payload"])


@respx.mock
async def test_monitor_initializes_lot_mfe_mae_on_first_observation(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)  # stop_loss=140, target_price=170
    await _seed_lot(repositories)  # no mfe/mae yet
    _mock_positions(qty="10", current_price="155")  # comfortably between stop/target -- no exit
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0
    lot = await _get_lot(repositories, "lot-1")
    assert lot.mfe_price == Decimal("155")
    assert lot.mae_price == Decimal("155")


@respx.mock
async def test_monitor_extends_mfe_on_a_new_high_and_leaves_mae_untouched(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    await _seed_lot(repositories, mfe_price="152", mae_price="148")
    _mock_positions(qty="10", current_price="160")  # a new high, still no breach
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    lot = await _get_lot(repositories, "lot-1")
    assert lot.mfe_price == Decimal("160")
    assert lot.mae_price == Decimal("148")  # unchanged -- 160 is not a new low


@respx.mock
async def test_monitor_does_not_narrow_existing_extrema(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    await _seed_lot(repositories, mfe_price="160", mae_price="145")
    _mock_positions(qty="10", current_price="150")  # strictly inside the existing [145, 160] range
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    lot = await _get_lot(repositories, "lot-1")
    assert lot.mfe_price == Decimal("160")
    assert lot.mae_price == Decimal("145")


@respx.mock
async def test_two_open_lots_for_same_asset_track_independent_extrema(tmp_path) -> None:
    """Direct regression test for tracking mfe/mae per-lot, not per-Holding
    -- a scale-in's second lot must never inherit the first lot's excursion
    history."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, quantity="20")
    await _seed_lot(repositories, lot_id="lot-1", opened_at=NOW, mfe_price="200", mae_price="150")
    await _seed_lot(repositories, lot_id="lot-2", opened_at=NOW, mfe_price=None, mae_price=None)  # freshly opened, no history yet
    _mock_positions(qty="20", current_price="155")
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    lot1 = await _get_lot(repositories, "lot-1")
    lot2 = await _get_lot(repositories, "lot-2")
    assert lot1.mfe_price == Decimal("200")  # untouched -- 155 doesn't beat its own prior high
    assert lot1.mae_price == Decimal("150")
    assert lot2.mfe_price == Decimal("155")  # its own first-ever observation
    assert lot2.mae_price == Decimal("155")


@respx.mock
async def test_monitor_never_updates_a_lot_already_fully_closed(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    await _seed_lot(repositories, quantity="10", remaining_quantity="0", mfe_price="152", mae_price="148")  # closed
    _mock_positions(qty="10", current_price="155")  # comfortably between stop/target -- no exit
    respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    lot = await _get_lot(repositories, "lot-1")
    assert lot.mfe_price == Decimal("152")  # unchanged -- a closed lot is never in open_lots_by_asset
    assert lot.mae_price == Decimal("148")


@respx.mock
async def test_long_position_breaching_stop_triggers_sell(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # below stop_loss=140
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "135")))
    _mock_fill_activities("act-1", "10", "135")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "ok"
    assert summary.positions_checked == 1
    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    request_body = order_route.calls[0].request.content
    assert b'"side":"sell"' in request_body
    assert b'"qty":"10"' in request_body


@respx.mock
async def test_long_position_breaching_target_triggers_sell(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="175")  # above target_price=170
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "175")))
    _mock_fill_activities("act-1", "10", "175")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1


@respx.mock
async def test_short_position_breaching_inverted_thresholds_triggers_buy(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    # short: stop is ABOVE entry, target is BELOW entry
    await _seed_holding(repositories, quantity="-10", stop_loss="160", target_price="130")
    _mock_positions(qty="-10", current_price="165")  # above stop_loss=160
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json=_order_json("accepted", "0", None, side="buy")))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(200, json=_order_json("filled", "10", "165", side="buy")))
    _mock_fill_activities("act-1", "10", "165", side="buy")

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    request_body = order_route.calls[0].request.content
    assert b'"side":"buy"' in request_body


@respx.mock
async def test_no_local_holding_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    _mock_positions(qty="10", current_price="100")  # would breach any threshold, but nothing on file
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.positions_checked == 1
    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_holding_with_no_thresholds_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, stop_loss=None, target_price=None)
    _mock_positions(qty="10", current_price="1")
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


def _aapl_call() -> AssetIdentity:
    return AssetIdentity(
        "AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000",
        metadata={"underlying_symbol": "AAPL", "expiry": "2026-08-25", "contract_multiplier": "100"},
    )


@respx.mock
async def test_option_within_forced_close_window_exits_even_without_breach(tmp_path) -> None:
    """An OPTION holding whose expiry falls within
    options_forced_close_days_before_expiry closes regardless of stop/
    target state -- NOW is 2026-08-24, expiry is 2026-08-25 (1 day out),
    LIMITS' threshold is 2 days -- current_price sits strictly between
    stop_loss and target_price so _breached alone would never trigger."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    holding = Holding(
        asset=_aapl_call(), quantity=Decimal("1"), average_price=Decimal("2.00"), updated_at=NOW,
        stop_loss=Decimal("1.00"), target_price=Decimal("5.00"),
    )
    await repositories.holdings.create_once(asset_identity_key(_aapl_call()), holding)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[{
        "symbol": "AAPL251219C00150000", "asset_class": "us_option", "qty": "1", "avg_entry_price": "2.00",
        "market_value": "0", "current_price": "2.50", "unrealized_pl": "0",
    }]))
    _mock_account()
    respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(200, json={"quotes": {"AAPL251219C00150000": {"bp": 2.49, "ap": 2.50, "t": QUOTE_TS}}})
    )
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "accepted", "symbol": "AAPL251219C00150000", "side": "sell", "filled_qty": "0", "filled_avg_price": None, "submitted_at": QUOTE_TS})
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "filled", "symbol": "AAPL251219C00150000", "side": "sell", "filled_qty": "1", "filled_avg_price": "2.50", "submitted_at": QUOTE_TS})
    )
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=[{
        "id": "act-1", "activity_type": "FILL", "symbol": "AAPL251219C00150000", "side": "sell",
        "qty": "1", "price": "2.50", "transaction_time": QUOTE_TS, "order_id": "order-1",
    }]))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    request_body = order_route.calls[0].request.content
    assert b'"side":"sell"' in request_body


@respx.mock
async def test_equity_holding_near_its_own_far_future_date_is_unaffected_by_near_expiry_check(tmp_path) -> None:
    """The near-expiry trigger is OPTION-only -- an equity holding with no
    breach and (obviously) no expiry metadata must never be force-closed."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)  # stop_loss=140, target_price=170
    _mock_positions(qty="10", current_price="155")  # comfortably between both
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_breach_with_in_flight_intent_is_skipped(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # would breach stop_loss=140
    in_flight = TradeIntent(
        "ti-scan-1", "idem-scan-1", "corr-1", _aapl(), Side.BUY, ExecutionMode.PAPER, "ai_scan", NOW,
        requested_quantity=Decimal("5"), status=TradeIntentStatus.ACCEPTED,
    )
    await repositories.trade_intents.create_once("ti-scan-1", in_flight, status=in_flight.status.value, unique_value=in_flight.idempotency_key)
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_broker_outage_reports_degraded_status_and_alerts(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(side_effect=httpx.ConnectError("connection refused"))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "degraded"
    assert summary.error is not None
    assert summary.positions_checked == 0
    assert summary.exits_triggered == 0


@respx.mock
async def test_unrecognized_broker_position_asset_class_reports_degraded_status(tmp_path) -> None:
    """A position with an asset_class this system doesn't recognize must
    fail the whole positions fetch (AlpacaDataIntegrityError) rather than
    being silently coerced into EQUITY -- this monitor's existing generic
    except-and-degrade path already covers it, same as any other
    positions-fetch failure."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[{
        "symbol": "WEIRD", "asset_class": "some_future_asset_class", "qty": "1", "avg_entry_price": "1",
        "market_value": "0", "current_price": "1", "unrealized_pl": "0",
    }]))

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "degraded"
    assert "BROKER_ASSET_CLASS_UNKNOWN" in summary.error


@respx.mock
async def test_option_with_invalid_expiry_metadata_alerts_but_keeps_stop_target_protection(tmp_path, caplog) -> None:
    """Missing/malformed expiry on an OPTION holding must never be silently
    treated as 'not near expiry' -- it's surfaced as a critical alert, but
    the position's ordinary stop/target protection (independent of expiry)
    keeps working."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    contract = AssetIdentity(
        "AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000",
        metadata={"underlying_symbol": "AAPL", "contract_multiplier": "100"},  # no "expiry" key at all
    )
    holding = Holding(asset=contract, quantity=Decimal("1"), average_price=Decimal("2.00"), updated_at=NOW, stop_loss=Decimal("1.00"))
    await repositories.holdings.create_once(asset_identity_key(contract), holding)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[{
        "symbol": "AAPL251219C00150000", "asset_class": "us_option", "qty": "1", "avg_entry_price": "2.00",
        "market_value": "0", "current_price": "0.90", "unrealized_pl": "0",  # below stop_loss=1.00
    }]))
    _mock_account()
    respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(200, json={"quotes": {"AAPL251219C00150000": {"bp": 0.89, "ap": 0.90, "t": QUOTE_TS}}})
    )
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "accepted", "symbol": "AAPL251219C00150000", "side": "sell", "filled_qty": "0", "filled_avg_price": None, "submitted_at": QUOTE_TS})
    )
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json={"id": "order-1", "status": "filled", "symbol": "AAPL251219C00150000", "side": "sell", "filled_qty": "1", "filled_avg_price": "0.90", "submitted_at": QUOTE_TS})
    )
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=[{
        "id": "act-1", "activity_type": "FILL", "symbol": "AAPL251219C00150000", "side": "sell",
        "qty": "1", "price": "0.90", "transaction_time": QUOTE_TS, "order_id": "order-1",
    }]))

    with caplog.at_level("WARNING"):
        summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1  # stop breach still protected the position
    assert order_route.call_count == 1
    skipped = [r for r in caplog.records if getattr(r, "event", None) == "telegram_alert_skipped_no_credentials"]
    assert any("OPTION_EXPIRY_METADATA_INVALID" in r.alert_message for r in skipped)


@respx.mock
async def test_breach_is_skipped_when_symbol_execution_lock_already_held(tmp_path) -> None:
    """A concurrent scan already holds the per-symbol execution reservation
    for AAPL -- the monitor must skip this breach cleanly rather than racing
    to submit its own exit order."""
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # breaches stop_loss=140
    _mock_account()
    _mock_quote()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    database = repositories.trade_intents.database
    assert await reserve_symbol_for_execution(database, _aapl(), "another-coordinator") is True

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0


@respx.mock
async def test_monitor_stops_starting_new_work_when_lease_already_lost(tmp_path) -> None:
    repositories, broker, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    _mock_positions(qty="10", current_price="135")  # would breach stop_loss=140
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200, json={}))

    lease_lost = asyncio.Event()
    lease_lost.set()

    summary = await run_position_monitor(repositories, broker, gateway, alerts, LIMITS, clock=lambda: NOW, lease_lost=lease_lost)
    await broker.aclose()

    assert summary.exits_triggered == 0
    assert order_route.call_count == 0
