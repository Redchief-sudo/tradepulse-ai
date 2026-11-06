"""Rev.149 follow-up R149-01 (review 2026-11-05): a filled BUY whose holding
has not been projected yet keeps its position slot.

Before the fix, position_keys was the union of holdings, quarantine, broker
positions and pending BUY intents. A BUY whose intent had become terminal
(FILLED, or CANCELED/EXPIRED after a partial fill) while its settlement was
still unprojected, and whose fill the broker did not show yet, was valued in
dollar exposure but held no slot. A different-symbol entry was then
approved with max_open_positions=1.
"""
from dataclasses import replace
from decimal import Decimal

import pytest
from test_rev149_risk_counts import (
    AAPL,
    NOW,
    TIGHT,
    _holding,
    _intent,
    _msft_buy,
    _opts,
    _position,
    _repositories,
    _snapshot,
)

from tradepulse.models import ExecutionMode, SettlementEvent, SettlementStatus, Side, TradeIntentStatus
from tradepulse.persistence import hydrate
from tradepulse.risk import evaluate_risk

UNPROJECTED = [SettlementStatus.PENDING, SettlementStatus.PROCESSING, SettlementStatus.RETRYABLE_FAILED,
               SettlementStatus.TERMINAL_FAILED, SettlementStatus.INTEGRITY_BLOCKED]


async def _settlement(repositories, event_id: str, side: Side, quantity: str, status: SettlementStatus, *,
                      intent_id: str = "ti-aapl", holding_projected: bool = False) -> None:
    done = status == SettlementStatus.COMPLETED
    event = SettlementEvent(event_id, f"fill-{event_id}", intent_id, AAPL, side, ExecutionMode.PAPER,
                            Decimal(quantity), Decimal(100), NOW, status=status, sector="Tech",
                            lot_projected=done, attribution_projected=done, cash_projected=done,
                            holding_projected=holding_projected or done, trade_projected=done,
                            integrity_verified=done)
    await repositories.settlements.create_once(event_id, event, status=event.status.value, unique_value=event_id)


def _refuses_for_slots(snapshot) -> bool:
    decision = evaluate_risk(_msft_buy(), snapshot, replace(TIGHT, max_daily_trades=10), _opts())
    return any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


@pytest.mark.parametrize(("intent_status", "quantity"), [
    (TradeIntentStatus.FILLED, "10"),
    (TradeIntentStatus.CANCELED, "4"),  # partially filled, then canceled
    (TradeIntentStatus.EXPIRED, "4"),   # partially filled, then expired
])
@pytest.mark.parametrize("settlement_status", UNPROJECTED)
async def test_a_terminal_buy_with_an_unprojected_fill_keeps_its_slot(
        tmp_path, intent_status, quantity, settlement_status) -> None:
    """The review's probes: no holding yet, and the broker's position read
    lags (empty). Settlement failure states keep the slot too, because the
    inventory is real whether or not the pipeline projected it."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, intent_status)
    await _settlement(repositories, "se-1", Side.BUY, quantity, settlement_status)

    snapshot = await _snapshot(repositories, [])

    assert snapshot.holdings_value == Decimal(quantity) * 100  # already counted in dollars
    assert snapshot.open_positions == 1
    assert _refuses_for_slots(snapshot)


async def test_the_slot_is_counted_once_through_every_stage_of_the_transition(tmp_path) -> None:
    """The same AAPL position seen by one, two or all of: the unprojected
    settlement, the broker, the holding. It is one slot at every stage."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.FILLED)
    await _settlement(repositories, "se-1", Side.BUY, "10", SettlementStatus.PENDING)
    broker = [_position(AAPL, "10", "1000", "100")]

    assert (await _snapshot(repositories, [])).open_positions == 1      # broker lags
    assert (await _snapshot(repositories, broker)).open_positions == 1  # broker catches up

    await _holding(repositories, AAPL, "10")  # projection written, flag not yet set
    assert (await _snapshot(repositories, broker)).open_positions == 1

    [row] = await repositories.settlements.list_all()
    projected = replace(hydrate("settlements", row["payload"]), status=SettlementStatus.COMPLETED,
                        lot_projected=True, attribution_projected=True, cash_projected=True,
                        holding_projected=True, trade_projected=True, integrity_verified=True)
    await repositories.settlements.update("se-1", projected, status=projected.status.value)
    assert (await _snapshot(repositories, broker)).open_positions == 1  # fully projected


async def test_a_closed_position_releases_its_slot(tmp_path) -> None:
    """Bought and sold, both settled, no holding and no broker position: the
    asset is proven flat and takes no slot."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.FILLED)
    await _intent(repositories, "ti-aapl-exit", AAPL, TradeIntentStatus.FILLED, side=Side.SELL)
    await _settlement(repositories, "se-1", Side.BUY, "10", SettlementStatus.COMPLETED, holding_projected=True)
    await _settlement(repositories, "se-2", Side.SELL, "10", SettlementStatus.COMPLETED,
                      intent_id="ti-aapl-exit", holding_projected=True)

    snapshot = await _snapshot(repositories, [])

    assert snapshot.open_positions == 0
    assert not _refuses_for_slots(snapshot)


async def test_an_unprojected_exit_alone_takes_no_slot(tmp_path) -> None:
    """Only positive BUY inventory reserves a slot; a sell settlement with
    no position behind it does not create one."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl-exit", AAPL, TradeIntentStatus.FILLED, side=Side.SELL)
    await _settlement(repositories, "se-2", Side.SELL, "10", SettlementStatus.PENDING, intent_id="ti-aapl-exit")

    assert (await _snapshot(repositories, [])).open_positions == 0
