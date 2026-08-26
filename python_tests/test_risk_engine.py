from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Holding, PortfolioSnapshot, Side
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.risk.engine import RiskCheckInput, RiskEvalOptions, build_portfolio_snapshot, check_max_drawdown, evaluate_risk

NOW = datetime(2026, 8, 15, tzinfo=UTC)
LIMITS = risk_limits_for_profile("balanced")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _seed_equity_snapshot(repositories: PersistenceRepositories, equity: Decimal, snapshot_id: str) -> None:
    snapshot = PortfolioSnapshot(
        snapshot_id=snapshot_id, as_of=NOW, total_equity=equity, cash_balance=Decimal("0"),
        holdings_value=Decimal("0"), sector_exposure={}, open_positions=0, outstanding_orders=0,
        trades_today=0, daily_pnl_pct=Decimal("0"), source="broker",
    )
    await repositories.equity_snapshots.create_once(snapshot_id, snapshot)


def _snapshot(**overrides):
    from tradepulse.models import PortfolioSnapshot

    defaults = dict(
        snapshot_id="snap-1", as_of=NOW, total_equity=Decimal("100000"), cash_balance=Decimal("50000"),
        holdings_value=Decimal("0"), sector_exposure={}, open_positions=0, outstanding_orders=0,
        trades_today=0, daily_pnl_pct=Decimal("0"), source="holdings",
    )
    defaults.update(overrides)
    return PortfolioSnapshot(**defaults)


def _buy(**overrides) -> RiskCheckInput:
    defaults = dict(
        symbol="AAPL", asset_class=AssetClass.EQUITY, side=Side.BUY,
        requested_quantity=Decimal("10"), price=Decimal("100"), confidence=Decimal("90"),
    )
    defaults.update(overrides)
    return RiskCheckInput(**defaults)


def test_kill_switch_denies_zero_shares_never_partial() -> None:
    decision = evaluate_risk(_buy(), _snapshot(), LIMITS, RiskEvalOptions(kill_switch=True))
    assert not decision.approved
    assert decision.approved_quantity == Decimal("0")
    assert decision.reasons == ["KILL_SWITCH_ACTIVE"]


def test_buy_rejected_when_cash_insufficient() -> None:
    """Regression test for the confirmed Base44 gap: reserveCash() existed in
    cashLedger.ts but was never called from execution.ts, so a buy could be
    approved with no pre-trade cash check. Here it is mandatory.
    """
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("50"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("10"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert not decision.approved
    assert any("INSUFFICIENT_CASH" in reason for reason in decision.reasons)


def test_buy_approved_when_cash_sufficient() -> None:
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("1"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert decision.approved


def test_sell_rejected_when_insufficient_position() -> None:
    sell = _buy(side=Side.SELL, requested_quantity=Decimal("10"))
    decision = evaluate_risk(sell, _snapshot(), LIMITS, RiskEvalOptions(held_quantity=Decimal("3")))
    assert not decision.approved
    assert "INSUFFICIENT_POSITION_TO_SELL" in decision.reasons[0]


def test_sell_approved_when_position_covers_request() -> None:
    sell = _buy(side=Side.SELL, requested_quantity=Decimal("3"))
    decision = evaluate_risk(sell, _snapshot(), LIMITS, RiskEvalOptions(held_quantity=Decimal("10")))
    assert decision.approved
    assert decision.approved_quantity == Decimal("3")


def test_buy_position_capped_to_max_position_pct() -> None:
    # balanced profile: max_position_pct=7% of 100000 equity = 7000 notional cap.
    # 200 shares @ $100 = $20,000 (20% of equity) exceeds the position cap but
    # stays under max_total_exposure_pct (40%), so this is capped, not
    # outright rejected the way a full-equity request would be.
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("200"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert decision.approved
    assert decision.approved_quantity * Decimal("100") <= Decimal("7000")
    assert any("MAX_POSITION_PCT" in reason for reason in decision.reasons)


def test_buy_rejected_outright_when_full_size_would_blow_total_exposure() -> None:
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("1000"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert not decision.approved
    assert any("MAX_TOTAL_EXPOSURE_EXCEEDED" in reason for reason in decision.reasons)


def test_buy_rejected_below_min_confidence() -> None:
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    decision = evaluate_risk(_buy(confidence=Decimal("10")), _snapshot(), LIMITS, opts)
    assert not decision.approved
    assert any("CONFIDENCE_BELOW_MIN" in reason for reason in decision.reasons)


def test_buy_rejected_when_daily_loss_limit_exceeded() -> None:
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    snapshot = _snapshot(daily_pnl_pct=Decimal("-2.0"))  # balanced limit is 1.0%
    decision = evaluate_risk(_buy(), snapshot, LIMITS, opts)
    assert not decision.approved
    assert any("MAX_DAILY_LOSS_EXCEEDED" in reason for reason in decision.reasons)


def test_buy_rejected_without_quote_data_for_spread_check() -> None:
    decision = evaluate_risk(_buy(), _snapshot(), LIMITS, RiskEvalOptions(available_cash=Decimal("100000")))
    assert not decision.approved
    assert "NO_QUOTE_DATA_FOR_SPREAD_CHECK" in decision.reasons


async def test_max_drawdown_breached_when_current_equity_far_below_historical_peak(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_equity_snapshot(repositories, Decimal("100000"), "snap-peak")
    limits = replace(LIMITS, max_drawdown_pct=Decimal("15"))

    check = await check_max_drawdown(repositories, Decimal("84000"), limits)

    assert check.breached is True
    assert check.peak_equity == Decimal("100000")
    assert check.drawdown_pct == Decimal("16")


async def test_max_drawdown_not_breached_within_limit(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_equity_snapshot(repositories, Decimal("100000"), "snap-peak")
    limits = replace(LIMITS, max_drawdown_pct=Decimal("15"))

    check = await check_max_drawdown(repositories, Decimal("90000"), limits)

    assert check.breached is False


async def test_max_drawdown_disabled_when_limit_is_zero(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_equity_snapshot(repositories, Decimal("100000"), "snap-peak")
    limits = replace(LIMITS, max_drawdown_pct=Decimal("0"))

    check = await check_max_drawdown(repositories, Decimal("10"), limits)

    assert check.breached is False
    assert check.peak_equity is None


async def test_max_drawdown_uses_current_equity_as_peak_when_no_history(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    limits = replace(LIMITS, max_drawdown_pct=Decimal("15"))

    check = await check_max_drawdown(repositories, Decimal("50000"), limits)

    assert check.breached is False
    assert check.peak_equity == Decimal("50000")


def _aapl() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def test_build_portfolio_snapshot_uses_mark_price_over_cost_basis_when_supplied(tmp_path) -> None:
    """Finding 2's fix: holdings_value/sector_exposure must reflect current
    market value (mark_prices), not the Holding's own cost-basis
    average_price, whenever a mark is supplied for that symbol."""
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW, sector="Tech")
    await repositories.holdings.create_once("equity:default:alpaca:AAPL", holding)

    snapshot = await build_portfolio_snapshot(
        repositories, cash_balance=Decimal("0"), mark_prices={"AAPL": Decimal("250")}, now=NOW,
    )

    assert snapshot.holdings_value == Decimal("2500")  # 10 * 250 (current mark), not 10 * 100 (cost basis)
    assert snapshot.sector_exposure["Tech"] == Decimal("2500")


async def test_build_portfolio_snapshot_falls_back_to_cost_basis_when_symbol_has_no_mark(tmp_path) -> None:
    """A symbol absent from mark_prices (e.g. the caller's positions fetch
    didn't include it) must not silently zero out its exposure -- falls back
    to the same average_price behavior as before this fix."""
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW)
    await repositories.holdings.create_once("equity:default:alpaca:AAPL", holding)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), mark_prices={"MSFT": Decimal("400")}, now=NOW)

    assert snapshot.holdings_value == Decimal("1000")  # 10 * 100 cost basis -- AAPL has no entry in mark_prices


async def test_build_portfolio_snapshot_defaults_to_cost_basis_when_mark_prices_omitted(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW)
    await repositories.holdings.create_once("equity:default:alpaca:AAPL", holding)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.holdings_value == Decimal("1000")
