"""Rev.149 follow-up R149-02 (review 2026-11-05): a damaged intent row for one
asset must not block protective exits on other assets.

Before the fix, the gateway's sell authorization hydrated every
trade_intents row in the database before filtering for the asset being
sold and the SELL side. One malformed historical row, for any asset or
side, raised inside every sell authorization, so every protective exit in
the cycle failed.

After the fix, only the side and asset of each row are decoded before its
relevance is decided. A damaged row that is a SELL (or whose side cannot be
read) for the asset being sold blocks that asset only. A non-BUY row whose
asset cannot be identified at all still blocks every sell, because it
cannot be proven unrelated to any of them.
"""
from decimal import Decimal

import httpx
import pytest
import respx
from test_monitor_coordinator import LIMITS, NOW, _aapl, _seed_holding, _seed_lot, _setup
from test_rev149_monitor_protection import (
    MSFT,
    _corrupt,
    _incidents,
    _mock_msft_sell,
    _seed_msft,
    _symbols_sold,
    _two_positions,
)

from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
)
from tradepulse.monitor import run_position_monitor

NVDA = AssetIdentity("NVDA", AssetClass.EQUITY, "alpaca:NVDA")


def _msft_position_only() -> None:
    rows = [{"symbol": "MSFT", "asset_class": "us_equity", "qty": "10", "avg_entry_price": "150",
             "market_value": "0", "current_price": "130", "unrealized_pl": "0"}]
    respx.get("https://paper-api.alpaca.markets/v2/positions").mock(return_value=httpx.Response(200, json=rows))


async def _damaged_intent(repositories, intent_id: str, asset: AssetIdentity, side: Side,
                          status: TradeIntentStatus, *, field: str = "reference_price", value: str = "0") -> None:
    intent = TradeIntent(intent_id, intent_id, intent_id, asset, side, ExecutionMode.PAPER, "test", NOW,
                         requested_quantity=Decimal(10), reference_price=Decimal(100), status=status)
    await repositories.trade_intents.create_once(intent_id, intent, status=status.value, unique_value=intent_id)
    await _corrupt(repositories, "trade_intents", intent_id, field, value)


async def _run(repositories, broker, market_data, gateway, alerts):
    summary = await run_position_monitor(repositories, broker, market_data, gateway, alerts, LIMITS, clock=lambda: NOW)
    await broker.aclose()
    return summary


@respx.mock
async def test_damaged_unrelated_terminal_buy_and_sell_intents_do_not_block_a_healthy_exit(tmp_path) -> None:
    """Both damaged rows belong to NVDA, which is not held. MSFT is below its
    stop and exits normally."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-nvda-buy", NVDA, Side.BUY, TradeIntentStatus.REJECTED)
    await _damaged_intent(repositories, "ti-nvda-sell", NVDA, Side.SELL, TradeIntentStatus.FILLED)
    _msft_position_only()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert _symbols_sold(order_route) == ["MSFT"]
    assert summary.exits_triggered == 1
    assert summary.status == "ok"
    assert await _incidents(repositories) == []


@respx.mock
async def test_the_reviews_probe_a_damaged_buy_on_a_held_asset_does_not_block_other_exits(tmp_path) -> None:
    """The review's probe: AAPL and MSFT both below their stops, and a
    historical REJECTED AAPL BUY with reference_price 0. A BUY is never sell
    evidence, so it blocks neither. MSFT's order route sees an MSFT order."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-aapl-old", _aapl(), Side.BUY, TradeIntentStatus.REJECTED)
    _msft_position_only()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert _symbols_sold(order_route) == ["MSFT"]
    assert summary.exits_triggered == 1
    assert await _incidents(repositories) == []


@respx.mock
async def test_a_damaged_pending_sell_keeps_its_own_asset_blocked_and_no_other(tmp_path) -> None:
    """AAPL has a damaged SUBMITTED sell. The monitor's in-flight rule (an SQL
    status-and-asset check, which never hydrates the row) already holds AAPL,
    exactly as for a healthy pending sell: no AAPL order. MSFT exits."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    await _seed_lot(repositories)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-aapl-exit", _aapl(), Side.SELL, TradeIntentStatus.SUBMITTED,
                          field="requested_quantity", value="not-a-number")
    _two_positions()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert _symbols_sold(order_route) == ["MSFT"]  # no AAPL order
    assert summary.exits_triggered == 1


@pytest.mark.parametrize(("field", "value"), [("requested_quantity", "not-a-number"), ("side", "garbled")])
@respx.mock
async def test_a_damaged_sell_record_on_the_asset_blocks_that_asset_and_no_other(tmp_path, field, value) -> None:
    """A FILLED AAPL sell whose payload is damaged (or whose side cannot be
    read) is sell evidence for AAPL that cannot be read: what it sold and
    whether it is fully settled are unknown. AAPL is not sold and is
    reported as a critical incident naming the row; MSFT exits."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_holding(repositories)
    await _seed_lot(repositories)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-aapl-exit", _aapl(), Side.SELL, TradeIntentStatus.FILLED,
                          field=field, value=value)
    _two_positions()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert _symbols_sold(order_route) == ["MSFT"]
    assert summary.exits_triggered == 1
    assert summary.status == "degraded"
    [incident] = await _incidents(repositories)
    assert incident["entity_id"] == asset_identity_key(_aapl())
    assert incident["severity"] == "critical"
    assert "SELL_EVIDENCE_DAMAGED" in incident["message"]
    assert "ti-aapl-exit" in incident["message"]


@respx.mock
async def test_a_sell_whose_asset_cannot_be_identified_blocks_every_sell(tmp_path) -> None:
    """An unidentifiable sell could belong to any asset, so no sell can be
    proven unaffected by it. This stays account-wide, and is reported."""
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-lost", NVDA, Side.SELL, TradeIntentStatus.SUBMITTED,
                          field="asset", value="unreadable")
    _msft_position_only()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert order_route.call_count == 0
    assert summary.status == "degraded"
    [incident] = await _incidents(repositories)
    assert incident["entity_id"] == asset_identity_key(MSFT)
    assert "SELL_EVIDENCE_UNIDENTIFIABLE" in incident["message"]
    assert "ti-lost" in incident["message"]


@respx.mock
async def test_a_buy_whose_asset_cannot_be_identified_does_not_block_sells(tmp_path) -> None:
    repositories, broker, market_data, gateway, alerts = await _setup(tmp_path)
    await _seed_msft(repositories)
    await _damaged_intent(repositories, "ti-lost-buy", NVDA, Side.BUY, TradeIntentStatus.REJECTED,
                          field="asset", value="unreadable")
    _msft_position_only()
    order_route = _mock_msft_sell()

    summary = await _run(repositories, broker, market_data, gateway, alerts)

    assert _symbols_sold(order_route) == ["MSFT"]
    assert summary.exits_triggered == 1
