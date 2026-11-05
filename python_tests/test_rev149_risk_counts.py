"""Rev.149 F03 and F06 (forensic audit 2026-11-04): count limits reserve
pending commitments, and account risk includes broker inventory that has no
local holding.

F03, before the fix: trades_today counted only BUY intents with a fill today,
open_positions was len(local holdings), and outstanding_orders left out
RISK_APPROVED. The portfolio lock serializes approvals, but it cannot enforce
caps whose counters omit commitments already approved.

F06, before the fix: the snapshot valued local holdings, quarantine records
and pending-epoch assets only. An ordinary broker position with no local
holding (inherited inventory with adoption off, or unmanaged inventory
before reconciliation) was neither exposure nor a position.
"""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.broker import AlpacaPosition
from tradepulse.config import risk_limits_for_profile
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    Holding,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.risk import RiskCheckInput, RiskEvalOptions, build_portfolio_snapshot, evaluate_risk
from tradepulse.risk.engine import UNMANAGED_SECTOR, open_portfolio_risk

NOW = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)  # a Monday, mid-session
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
MSFT = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
CALL = AssetIdentity("AAPL260918C00150000", AssetClass.OPTION, "alpaca:AAPL260918C00150000",
                     metadata={"underlying_symbol": "AAPL", "expiry": "2026-09-18", "contract_multiplier": "100"})
TIGHT = replace(risk_limits_for_profile("balanced"), max_daily_trades=1, max_open_positions=1, max_simultaneous_orders=1)


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _intent(repositories, intent_id: str, asset: AssetIdentity, status: TradeIntentStatus, *,
                  side: Side = Side.BUY, quantity: str = "10") -> None:
    intent = TradeIntent(intent_id, intent_id, intent_id, asset, side, ExecutionMode.PAPER, "test", NOW,
                         requested_quantity=Decimal(quantity), reference_price=Decimal(100), status=status)
    await repositories.trade_intents.create_once(intent_id, intent, status=status.value, unique_value=intent_id)


async def _holding(repositories, asset: AssetIdentity, quantity: str, sector: str = "Tech") -> None:
    await repositories.holdings.create_once(asset_identity_key(asset), Holding(
        asset=asset, quantity=Decimal(quantity), average_price=Decimal(100), updated_at=NOW,
        stop_loss=Decimal(95), sector=sector))


def _position(asset: AssetIdentity, qty: str, market_value: str, price: str) -> AlpacaPosition:
    return AlpacaPosition(symbol=asset.symbol, asset_class=asset.asset_class, qty=Decimal(qty),
                          avg_entry_price=Decimal(price), market_value=Decimal(market_value),
                          current_price=Decimal(price), unrealized_pl=Decimal(0))


async def _snapshot(repositories, positions: list[AlpacaPosition] | None = None, *,
                    equity: str = "100000", cash: str = "50000"):
    by_key = {asset_identity_key(AssetIdentity(p.symbol, p.asset_class, f"alpaca:{p.symbol}")): p
              for p in positions or []}
    return await build_portfolio_snapshot(
        repositories, cash_balance=Decimal(cash), account_equity=Decimal(equity),
        broker_prev_close_equity=Decimal(equity), mark_prices={k: p.current_price for k, p in by_key.items()},
        broker_positions=by_key, now=NOW)


def _msft_buy(quantity: str = "10") -> RiskCheckInput:
    return RiskCheckInput(symbol="MSFT", asset_class=AssetClass.EQUITY, side=Side.BUY,
                          requested_quantity=Decimal(quantity), price=Decimal(100), confidence=Decimal(90),
                          stop_loss=Decimal(95), sector="Tech")


def _opts(cash: str = "50000") -> RiskEvalOptions:
    """A buy that passes every quote check, so a refusal is about the limit under test."""
    return RiskEvalOptions(available_cash=Decimal(cash), bid=Decimal("99.99"), ask=Decimal(100),
                           estimated_slippage_pct=Decimal("0.01"))


async def test_control_the_test_buy_is_approved_on_an_empty_account(tmp_path) -> None:
    """Without this, a refusal below could come from the test input itself."""
    repositories = await _repositories(tmp_path)

    decision = evaluate_risk(_msft_buy(), await _snapshot(repositories), TIGHT, _opts())

    assert decision.approved, decision.reasons


# --- F03 --------------------------------------------------------------------

@pytest.mark.parametrize("status", [TradeIntentStatus.RISK_APPROVED, TradeIntentStatus.ACCEPTED])
async def test_a_pending_buy_uses_a_daily_entry_and_a_position_slot(tmp_path, status) -> None:
    """The audit's probe: one pending AAPL buy, limits of one daily entry and
    one open position. Before the fix, MSFT was approved."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, status)

    snapshot = await _snapshot(repositories)
    decision = evaluate_risk(_msft_buy(), snapshot, TIGHT, _opts())

    assert snapshot.trades_today == 1
    assert snapshot.open_positions == 1
    assert not decision.approved
    assert any(r.startswith("MAX_DAILY_TRADES_REACHED") for r in decision.reasons)
    assert any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


async def test_an_approved_unsent_buy_uses_dispatch_capacity(tmp_path) -> None:
    """The audit's third probe: RISK_APPROVED left outstanding_orders at zero."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.RISK_APPROVED)
    limits = replace(risk_limits_for_profile("balanced"), max_simultaneous_orders=1)

    snapshot = await _snapshot(repositories)
    decision = evaluate_risk(_msft_buy(), snapshot, limits, _opts())

    assert snapshot.outstanding_orders == 1
    assert any(r.startswith("MAX_SIMULTANEOUS_ORDERS_REACHED") for r in decision.reasons)


@pytest.mark.parametrize("status", [TradeIntentStatus.REJECTED, TradeIntentStatus.CANCELED])
async def test_a_definitively_closed_buy_releases_its_capacity(tmp_path, status) -> None:
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, status)

    snapshot = await _snapshot(repositories)

    assert (snapshot.trades_today, snapshot.open_positions, snapshot.outstanding_orders) == (0, 0, 0)


async def test_an_unknown_submission_keeps_its_capacity(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.SUBMISSION_UNKNOWN)

    snapshot = await _snapshot(repositories)

    assert (snapshot.trades_today, snapshot.open_positions, snapshot.outstanding_orders) == (1, 1, 1)


async def test_a_partially_filled_buy_is_counted_once(tmp_path) -> None:
    """Filled today and still pending: one daily entry, one position (its
    holding and its intent are the same asset), one outstanding order."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.PARTIALLY_FILLED)
    await _holding(repositories, AAPL, "4")
    fill = Fill("fill-1", "ti-aapl", "order-1", AAPL, Side.BUY, ExecutionMode.PAPER,
                Decimal(4), Decimal(100), Decimal(0), Decimal(0), NOW)
    await repositories.fills.create_once("fill-1", fill, unique_value=None)

    snapshot = await _snapshot(repositories, [_position(AAPL, "4", "400", "100")])

    assert (snapshot.trades_today, snapshot.open_positions, snapshot.outstanding_orders) == (1, 1, 1)


async def test_an_add_on_to_a_held_asset_does_not_take_a_second_position_slot(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _holding(repositories, AAPL, "10")
    await _intent(repositories, "ti-aapl-add", AAPL, TradeIntentStatus.RISK_APPROVED)

    snapshot = await _snapshot(repositories, [_position(AAPL, "10", "1000", "100")])

    assert snapshot.open_positions == 1
    assert snapshot.trades_today == 1  # an add-on is still an entry


async def test_a_pending_exit_is_not_an_entry_or_a_position_slot(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _holding(repositories, AAPL, "10")
    await _intent(repositories, "ti-aapl-exit", AAPL, TradeIntentStatus.SUBMITTED, side=Side.SELL)

    snapshot = await _snapshot(repositories, [_position(AAPL, "10", "1000", "100")])

    assert snapshot.trades_today == 0
    assert snapshot.open_positions == 1
    assert snapshot.outstanding_orders == 1  # it is still an order at the broker


# --- F06 --------------------------------------------------------------------

async def test_broker_only_inventory_is_account_exposure_and_a_position(tmp_path) -> None:
    """The audit's probe: $100,000 equity, $10,000 cash, a broker-only
    $90,000 AAPL position. Before the fix, holdings value and positions were
    zero, and a MSFT buy passed the 60% total-exposure cap."""
    repositories = await _repositories(tmp_path)

    snapshot = await _snapshot(repositories, [_position(AAPL, "900", "90000", "100")], cash="10000")
    # The sector cap is lifted so the binding check is the 60% total cap the
    # audit names (unmanaged exposure also counts against every sector cap).
    limits = replace(risk_limits_for_profile("balanced"), max_sector_pct=Decimal(100))
    decision = evaluate_risk(_msft_buy("50"), snapshot, limits, _opts("10000"))

    assert snapshot.holdings_value == Decimal(90000)
    assert snapshot.open_positions == 1
    assert snapshot.sector_exposure[UNMANAGED_SECTOR] == Decimal(90000)
    assert not decision.approved
    assert any("TOTAL_EXPOSURE" in r for r in decision.reasons), decision.reasons


async def test_partially_adopted_inventory_counts_only_the_unheld_excess(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _holding(repositories, AAPL, "4")

    snapshot = await _snapshot(repositories, [_position(AAPL, "10", "1000", "100")])

    assert snapshot.holdings_value == Decimal(1000)  # 4 held at the mark + 6 unheld
    assert snapshot.sector_exposure["Tech"] == Decimal(400)
    assert snapshot.sector_exposure[UNMANAGED_SECTOR] == Decimal(600)
    assert snapshot.open_positions == 1


async def test_a_filled_but_unsettled_buy_is_not_counted_twice(tmp_path) -> None:
    """The broker already shows the fill; settlement has not projected the
    holding. The local model (the unsettled event) covers it."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-aapl", AAPL, TradeIntentStatus.ACCEPTED)
    event = SettlementEvent("se-1", "fill-1", "ti-aapl", AAPL, Side.BUY, ExecutionMode.PAPER,
                            Decimal(10), Decimal(100), NOW, status=SettlementStatus.PENDING, sector="Tech")
    await repositories.settlements.create_once("se-1", event, status=event.status.value, unique_value="se-1")

    snapshot = await _snapshot(repositories, [_position(AAPL, "10", "1000", "100")])

    assert snapshot.holdings_value == Decimal(1000)
    assert UNMANAGED_SECTOR not in snapshot.sector_exposure


async def test_broker_only_option_inventory_uses_its_market_value(tmp_path) -> None:
    repositories = await _repositories(tmp_path)

    snapshot = await _snapshot(repositories, [_position(CALL, "2", "500", "2.50")])

    assert snapshot.holdings_value == Decimal(500)  # 2 contracts x 2.50 x 100
    assert snapshot.open_positions == 1


async def test_unmanaged_exposure_counts_against_every_sector_cap(tmp_path) -> None:
    """Unmanaged inventory has no known sector, so it is counted as if it
    were in the candidate's own sector: never a free pass."""
    repositories = await _repositories(tmp_path)
    limits = risk_limits_for_profile("balanced")
    # Unmanaged exposure alone already fills the sector cap.
    unmanaged_value = limits.max_sector_pct * 1000  # % of $100,000 equity
    snapshot = await _snapshot(repositories, [_position(AAPL, str(unmanaged_value / 100), str(unmanaged_value), "100")])

    decision = evaluate_risk(_msft_buy("10"), snapshot, limits, _opts())

    assert not decision.approved
    assert any("MAX_SECTOR_PCT" in r for r in decision.reasons), decision.reasons


async def test_broker_only_inventory_counts_its_whole_value_as_open_risk(tmp_path) -> None:
    """No local stop protects it, so, like a holding with no stop, its whole
    notional is at risk."""
    repositories = await _repositories(tmp_path)
    await _holding(repositories, MSFT, "10")  # stop 95 under 100: risks 50

    positions = [_position(AAPL, "10", "1000", "100"), _position(MSFT, "10", "1000", "100")]
    by_key = {asset_identity_key(AssetIdentity(p.symbol, p.asset_class, f"alpaca:{p.symbol}")): p for p in positions}

    assert await open_portfolio_risk(repositories) == Decimal(50)
    assert await open_portfolio_risk(repositories, broker_positions=by_key) == Decimal(1050)
