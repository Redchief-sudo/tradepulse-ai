"""Rev.149 F01 (forensic audit 2026-11-04): an unchanged ratchet must never
suppress a position's exit checks.

Before the fix, _ratchet_stop returned None both when the holding vanished
and when the stop simply did not move, and the monitor skipped the position
on either. Once break-even or a trailing stop was set, every cycle that did
not raise it again skipped the stop, target, expiry, time and stall checks.

Each test here has an already-set current_stop that the cycle does not
raise, and expects the exit. The last test is the control: a holding that
really is deleted during the cycle gets no exit.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import respx
from test_monitor_coordinator import (
    LIMITS,
    NOW,
    QUOTE_TS,
    _aapl,
    _mock_account,
    _mock_fill_activities,
    _mock_positions,
    _mock_quote,
    _order_json,
    _seed_holding,
    _seed_lot,
    _setup,
)

from tradepulse.models import AssetClass, AssetIdentity, Holding, PositionLot, asset_identity_key
from tradepulse.monitor import run_position_monitor


def _mock_sell(qty: str, price: str) -> respx.Route:
    _mock_account()
    _mock_quote()
    route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(
        return_value=httpx.Response(200, json=_order_json("accepted", "0", None)))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(
        return_value=httpx.Response(200, json=_order_json("filled", qty, price)))
    _mock_fill_activities("act-1", qty, price)
    return route


async def _exit_reason(repositories) -> str:
    [intent_row] = await repositories.trade_intents.list_all()
    return intent_row["payload"]["risk_snapshot"]["exit_reason"]


@respx.mock
async def test_unchanged_break_even_stop_still_exits_when_breached(tmp_path) -> None:
    """The audit's counterexample: entry 150, stop 140, break-even stop 150,
    mark 130. Both stops are breached."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="150")
    await _seed_lot(repositories)
    _mock_positions(qty="10", current_price="130")
    order_route = _mock_sell("10", "130")

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "ok"
    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert b'"side":"sell"' in order_route.calls[0].request.content
    assert await _exit_reason(repositories) == "break_even_stop"


@respx.mock
async def test_unchanged_trailing_stop_still_exits_when_breached(tmp_path) -> None:
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="155")
    await _seed_lot(repositories)  # no MFE, so the candidate (150) cannot raise 155
    _mock_positions(qty="10", current_price="130")
    order_route = _mock_sell("10", "130")

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert await _exit_reason(repositories) == "trailing_stop"


@respx.mock
async def test_unchanged_stop_does_not_suppress_the_target(tmp_path) -> None:
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="160", target_price="170")
    await _seed_lot(repositories)  # no MFE: the candidate is 150, below 160
    _mock_positions(qty="10", current_price="175")
    respx.get("https://data.alpaca.markets/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": []}))
    order_route = _mock_sell("10", "175")

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert await _exit_reason(repositories) == "target_price"


@respx.mock
async def test_unchanged_stop_does_not_suppress_the_time_stop(tmp_path) -> None:
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="150", target_price="999")
    await _seed_lot(repositories, opened_at=NOW - timedelta(days=LIMITS.max_hold_days))
    _mock_positions(qty="10", current_price="152")  # above the stop, below the target
    order_route = _mock_sell("10", "152")

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert await _exit_reason(repositories) == "time_stop"


@respx.mock
async def test_unchanged_stop_does_not_suppress_the_stall_exit(tmp_path) -> None:
    limits = replace(LIMITS, stall_exit_days=7, stall_min_r=Decimal("0.5"), max_hold_days=0)
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="150", target_price="999")
    await _seed_lot(repositories, opened_at=NOW - timedelta(days=8))
    _mock_positions(qty="10", current_price="150.5")  # 0.05 R: never reached stall_min_r
    order_route = _mock_sell("10", "150.5")

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, limits, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert await _exit_reason(repositories) == "stall"


def _call() -> AssetIdentity:
    return AssetIdentity(
        "AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000",
        metadata={"underlying_symbol": "AAPL", "expiry": "2026-08-25", "contract_multiplier": "100"},
    )


@respx.mock
async def test_unchanged_option_break_even_stop_does_not_suppress_near_expiry_close(tmp_path) -> None:
    """Options have no ATR trail, so a break-even option stop is unchanged on
    every cycle after it is set. Expiry is one day out (the window is two)."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    asset = _call()
    await repositories.holdings.create_once(asset_identity_key(asset), Holding(
        asset=asset, quantity=Decimal("1"), average_price=Decimal("2.00"), updated_at=NOW,
        stop_loss=Decimal("1.00"), target_price=Decimal("5.00"), current_stop=Decimal("2.00"),
    ))
    lot = PositionLot(lot_id="lot-call", originating_fill_id="fill-lot-call", asset=asset, position_side="long",
                      opened_quantity=Decimal("1"), remaining_quantity=Decimal("1"),
                      acquisition_price=Decimal("2.00"), opened_at=NOW)
    await repositories.position_lots.create_once(lot.lot_id, lot, unique_value=lot.originating_fill_id)
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=[{
        "symbol": asset.symbol, "asset_class": "us_option", "qty": "1", "avg_entry_price": "2.00",
        "market_value": "0", "current_price": "2.50", "unrealized_pl": "0",
    }]))
    _mock_account()
    respx.get("https://data.alpaca.markets/v1beta1/options/quotes/latest").mock(
        return_value=httpx.Response(200, json={"quotes": {asset.symbol: {"bp": 2.49, "ap": 2.50, "t": QUOTE_TS}}}))
    order = {"id": "order-1", "symbol": asset.symbol, "side": "sell", "submitted_at": QUOTE_TS}
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(
        200, json={**order, "status": "accepted", "filled_qty": "0", "filled_avg_price": None}))
    respx.get("https://paper-api.alpaca.markets/v2/orders/order-1").mock(return_value=httpx.Response(
        200, json={**order, "status": "filled", "filled_qty": "1", "filled_avg_price": "2.50"}))
    respx.get("https://paper-api.alpaca.markets/v2/account/activities").mock(return_value=httpx.Response(200, json=[{
        "id": "act-1", "activity_type": "FILL", "symbol": asset.symbol, "side": "sell",
        "qty": "1", "price": "2.50", "transaction_time": QUOTE_TS, "order_id": "order-1",
    }]))

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.exits_triggered == 1
    assert order_route.call_count == 1
    assert await _exit_reason(repositories) == "option_expiry"


@respx.mock
async def test_holding_deleted_during_the_cycle_gets_no_exit(tmp_path, monkeypatch) -> None:
    """Control: settlement closes the holding between the monitor's read and
    the ratchet's transaction. That is the only case the skip is for."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories, current_stop="150")
    await _seed_lot(repositories)
    _mock_positions(qty="10", current_price="130")
    order_route = _mock_sell("10", "130")
    original_mutate = repositories.holdings.mutate

    async def delete_then_mutate(record_id, decide, **kwargs):
        await repositories.holdings.delete(record_id)
        return await original_mutate(record_id, decide, **kwargs)

    monkeypatch.setattr(repositories.holdings, "mutate", delete_then_mutate)

    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()

    assert summary.status == "ok"
    assert summary.exits_triggered == 0
    assert order_route.call_count == 0
    assert await repositories.holdings.get(asset_identity_key(_aapl())) is None
