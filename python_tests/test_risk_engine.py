from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.config import risk_limits_for_profile
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Holding,
    PortfolioSnapshot,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.risk.engine import RiskCheckInput, RiskEvalOptions, build_portfolio_snapshot, check_cash_sufficiency, check_max_drawdown, evaluate_risk

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
    approved with no pre-trade cash check. Here it is mandatory. Cash is
    below min_lot_notional ($1 default) so even the soft cash-sizing cap
    can't produce anything executable -- still a genuine rejection.
    """
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("0.50"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("10"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert not decision.approved
    assert any("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in reason for reason in decision.reasons)


def test_buy_with_partial_cash_sizes_down_instead_of_rejecting() -> None:
    """Enough cash for some shares, not the full request -- sizes down,
    never rejects outright (small-account support)."""
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("495"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("10"), price=Decimal("100")), _snapshot(), LIMITS, opts)
    assert decision.approved
    assert Decimal("0") < decision.approved_quantity < Decimal("10")
    assert any("BY_AVAILABLE_CASH" in reason for reason in decision.reasons)


def test_protective_sell_ignores_available_cash() -> None:
    sell = _buy(side=Side.SELL, requested_quantity=Decimal("5"))
    opts = RiskEvalOptions(protective_exit=True, held_quantity=Decimal("5"), available_cash=Decimal("0.01"))
    decision = evaluate_risk(sell, _snapshot(), LIMITS, opts)
    assert decision.approved
    assert decision.approved_quantity == Decimal("5")


def test_plain_covered_sell_ignores_available_cash() -> None:
    sell = _buy(side=Side.SELL, requested_quantity=Decimal("5"))
    opts = RiskEvalOptions(held_quantity=Decimal("5"), available_cash=Decimal("0.01"))
    decision = evaluate_risk(sell, _snapshot(), LIMITS, opts)
    assert decision.approved
    assert decision.approved_quantity == Decimal("5")


def test_confidence_scales_risk_budget_not_just_quantity() -> None:
    """Confidence modifies the ALLOWED RISK BUDGET (not requested quantity
    directly) -- verify the budget-level invariant, not just the resulting
    approved_quantity."""
    stop_loss = Decimal("96")  # risk_per_share = 4 against price=100
    total_equity = Decimal("100000")
    base_risk_budget = (LIMITS.max_risk_per_trade_pct / 100) * total_equity
    adjusted_risk_budget = base_risk_budget * LIMITS.min_position_size_multiplier

    buy = _buy(confidence=LIMITS.min_confidence, stop_loss=stop_loss, requested_quantity=Decimal("300"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(total_equity=total_equity), LIMITS, opts)

    risk_per_share = Decimal("100") - stop_loss
    assert decision.approved
    assert decision.approved_quantity * risk_per_share <= adjusted_risk_budget
    assert adjusted_risk_budget <= base_risk_budget
    assert any("RISK_BUDGET_SCALED_BY_CONFIDENCE" in r for r in decision.reasons)


def test_confidence_at_100_uses_full_unscaled_risk_budget() -> None:
    stop_loss = Decimal("96")
    total_equity = Decimal("100000")
    base_risk_budget = (LIMITS.max_risk_per_trade_pct / 100) * total_equity
    buy = _buy(confidence=Decimal("100"), stop_loss=stop_loss, requested_quantity=Decimal("300"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(total_equity=total_equity), LIMITS, opts)

    risk_per_share = Decimal("100") - stop_loss
    assert decision.approved
    assert decision.approved_quantity * risk_per_share <= base_risk_budget
    assert not any("RISK_BUDGET_SCALED_BY_CONFIDENCE" in r for r in decision.reasons)


def test_confidence_none_does_not_scale_risk_budget() -> None:
    buy = _buy(confidence=None, stop_loss=Decimal("96"), requested_quantity=Decimal("300"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(), LIMITS, opts)
    assert decision.approved
    assert not any("RISK_BUDGET_SCALED_BY_CONFIDENCE" in r for r in decision.reasons)


def test_min_lot_notional_rejects_tiny_notional_even_when_quantity_floor_passes() -> None:
    """A low-priced asset where a few units clear the unit-quantity floor
    (0.001) but not the dollar floor (min_lot_notional, $1 default)."""
    buy = _buy(price=Decimal("0.01"), requested_quantity=Decimal("50"), confidence=None)  # 50 * $0.01 = $0.50 < $1
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("1000"))
    decision = evaluate_risk(buy, _snapshot(), LIMITS, opts)
    assert not decision.approved
    assert any("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in r for r in decision.reasons)


def test_all_caps_combined_only_ever_shrink_never_enlarge() -> None:
    """A high-confidence, low-risk request already at/under every cap comes
    back completely unchanged -- proves the non-negotiable rule holds
    through all sizing paths combined, not just individually."""
    buy = _buy(confidence=Decimal("100"), requested_quantity=Decimal("1"), price=Decimal("100"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("1000000"))
    decision = evaluate_risk(buy, _snapshot(), LIMITS, opts)
    assert decision.approved
    assert decision.approved_quantity == Decimal("1")


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


def test_buy_sizes_down_when_requested_size_exceeds_remaining_total_exposure() -> None:
    """Total-exposure headroom is the binding constraint -- must size DOWN
    to whatever remains, never reject outright while capacity still exists.
    Existing holdings already consume most of the balanced profile's 40% of
    $100k = $40k exposure budget, leaving only $2k of headroom -- well under
    what max_position_pct ($7k) alone would allow, so exposure is what
    actually binds here."""
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    snapshot = _snapshot(holdings_value=Decimal("38000"))
    decision = evaluate_risk(_buy(requested_quantity=Decimal("50"), price=Decimal("100")), snapshot, LIMITS, opts)
    assert decision.approved
    assert decision.approved_quantity * Decimal("100") <= Decimal("2000")
    assert any("BY_MAX_TOTAL_EXPOSURE" in reason for reason in decision.reasons)


def test_buy_rejected_when_no_total_exposure_capacity_remains() -> None:
    """Exposure already AT the cap -- capped quantity resolves to zero,
    rejected via the generic minimum-lot floor, not a separate
    exposure-specific early reject."""
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))
    snapshot = _snapshot(holdings_value=Decimal("40000"))  # already at the 40% cap -- zero headroom
    decision = evaluate_risk(_buy(requested_quantity=Decimal("50"), price=Decimal("100")), snapshot, LIMITS, opts)
    assert not decision.approved
    assert any("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in reason for reason in decision.reasons)


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


def _msft() -> AssetIdentity:
    return AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")


def _aapl_crypto() -> AssetIdentity:
    """Same ticker text as _aapl() -- proves mark_prices lookups are
    identity-safe, not bare-symbol (a collision the Rev.65 gateway fix
    itself was found to still have)."""
    return AssetIdentity("AAPL", AssetClass.CRYPTO, "alpaca:AAPL")


async def test_build_portfolio_snapshot_uses_mark_price_over_cost_basis_when_supplied(tmp_path) -> None:
    """Finding 2's fix: holdings_value/sector_exposure must reflect current
    market value (mark_prices), not the Holding's own cost-basis
    average_price, whenever a mark is supplied for that asset."""
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW, sector="Tech")
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)

    snapshot = await build_portfolio_snapshot(
        repositories, cash_balance=Decimal("0"), mark_prices={asset_identity_key(_aapl()): Decimal("250")}, now=NOW,
    )

    assert snapshot.holdings_value == Decimal("2500")  # 10 * 250 (current mark), not 10 * 100 (cost basis)
    assert snapshot.sector_exposure["Tech"] == Decimal("2500")


async def test_build_portfolio_snapshot_falls_back_to_cost_basis_when_asset_has_no_mark(tmp_path) -> None:
    """An asset absent from mark_prices (e.g. the caller's positions fetch
    didn't include it) must not silently zero out its exposure -- falls back
    to the same average_price behavior as before this fix."""
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW)
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)

    snapshot = await build_portfolio_snapshot(
        repositories, cash_balance=Decimal("0"), mark_prices={asset_identity_key(_msft()): Decimal("400")}, now=NOW
    )

    assert snapshot.holdings_value == Decimal("1000")  # 10 * 100 cost basis -- AAPL has no entry in mark_prices


async def test_build_portfolio_snapshot_defaults_to_cost_basis_when_mark_prices_omitted(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW)
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.holdings_value == Decimal("1000")


async def test_build_portfolio_snapshot_mark_prices_do_not_collide_across_asset_classes(tmp_path) -> None:
    """Two holdings sharing display-symbol text ('AAPL') but different asset
    classes each get their OWN mark price from a canonically-keyed
    mark_prices dict -- proves no bare-symbol collision."""
    repositories = await _repositories(tmp_path)
    equity_holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW, sector="Tech")
    crypto_holding = Holding(asset=_aapl_crypto(), quantity=Decimal("2"), average_price=Decimal("50000"), updated_at=NOW, sector="Crypto")
    await repositories.holdings.create_once(asset_identity_key(_aapl()), equity_holding)
    await repositories.holdings.create_once(asset_identity_key(_aapl_crypto()), crypto_holding)

    snapshot = await build_portfolio_snapshot(
        repositories, cash_balance=Decimal("0"),
        mark_prices={asset_identity_key(_aapl()): Decimal("250"), asset_identity_key(_aapl_crypto()): Decimal("60000")},
        now=NOW,
    )

    assert snapshot.holdings_value == Decimal("2500") + Decimal("120000")  # 10*250 + 2*60000, not cross-contaminated
    assert snapshot.sector_exposure["Tech"] == Decimal("2500")
    assert snapshot.sector_exposure["Crypto"] == Decimal("120000")


async def _seed_intent(repositories: PersistenceRepositories, *, trade_intent_id: str, status: TradeIntentStatus) -> None:
    intent = TradeIntent(
        trade_intent_id, trade_intent_id, trade_intent_id, _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("5"), status=status,
    )
    await repositories.trade_intents.create_once(trade_intent_id, intent, status=status.value, unique_value=trade_intent_id)


async def test_build_portfolio_snapshot_counts_submission_unknown_as_outstanding(tmp_path) -> None:
    """Finding 3: an intent whose broker outcome is genuinely unresolved
    (never blind-resubmitted, per execute_intent's own recovery logic) must
    count toward max_simultaneous_orders -- it may still be a live broker
    order."""
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, trade_intent_id="ti-unknown", status=TradeIntentStatus.SUBMISSION_UNKNOWN)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.outstanding_orders == 1


async def test_build_portfolio_snapshot_does_not_count_risk_approved_as_outstanding(tmp_path) -> None:
    """RISK_APPROVED hasn't reached the broker yet -- must not count as
    outstanding broker exposure."""
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, trade_intent_id="ti-approved", status=TradeIntentStatus.RISK_APPROVED)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.outstanding_orders == 0


async def test_pending_risk_approved_intent_reserves_capacity_for_the_next_snapshot(tmp_path) -> None:
    """The sequential-handoff case the portfolio-risk lock alone does NOT
    cover: caller A evaluates risk, gets RISK_APPROVED, and releases the
    lock -- all before its order ever reaches the broker, let alone fills.
    A fresh build_portfolio_snapshot (as caller B's own risk evaluation would
    call) must already see A's committed notional as exposure, so B is sized
    down or rejected against the REMAINING capacity, not the full cap, even
    though A's TradeIntent hasn't settled into a real Holding yet."""
    repositories = await _repositories(tmp_path)
    # max_sector_pct raised well above what this test's $38k Tech position
    # would trip, so only max_total_exposure_pct (40% of $100k = $40k) binds
    # -- isolates the assertion to the exposure reservation this test is
    # actually about.
    limits = replace(LIMITS, max_total_exposure_pct=Decimal("40"), max_sector_pct=Decimal("90"))
    # Caller A: already RISK_APPROVED for $38,000 of AAPL, no fill yet -- no
    # Holding row exists, so only the pending-notional reservation can
    # possibly account for this.
    approved_a = TradeIntent(
        "ti-a", "ti-a", "ti-a", _aapl(), Side.BUY, ExecutionMode.PAPER, "test", NOW,
        requested_quantity=Decimal("380"), reference_price=Decimal("100"), sector="Tech",
        status=TradeIntentStatus.RISK_APPROVED,
    )
    await repositories.trade_intents.create_once("ti-a", approved_a, status=TradeIntentStatus.RISK_APPROVED.value, unique_value="ti-a")

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), account_equity=Decimal("100000"), now=NOW)

    # Proves the reservation is visible on a completely fresh snapshot read
    # -- not carried through in-memory state from caller A's own evaluation.
    assert snapshot.holdings_value == Decimal("38000")
    assert snapshot.sector_exposure["Tech"] == Decimal("38000")

    # Caller B: a second, same-sector candidate that would easily fit within
    # the FULL $40k cap (it only asks for $5k) but must still be evaluated
    # against the $2k that's actually left once A's reservation is honored.
    candidate_b = _buy(requested_quantity=Decimal("50"), price=Decimal("100"), sector="Tech")
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("100000"))

    decision = evaluate_risk(candidate_b, snapshot, limits, opts)

    assert decision.approved
    assert decision.approved_quantity * Decimal("100") <= Decimal("2000")
    assert any("BY_MAX_TOTAL_EXPOSURE" in reason for reason in decision.reasons)


def test_all_dynamic_controls_combine_and_still_approve_a_smaller_but_valid_quantity() -> None:
    """Every dynamic control acting together -- confidence-adjusted risk
    budget, position/sector/total-exposure caps, and cash -- still leaves an
    executable quantity and approves it. The strongest proof of the
    allocator as a WHOLE, not just each control verified in isolation."""
    limits = replace(
        LIMITS, max_risk_per_trade_pct=Decimal("1.0"), max_position_pct=Decimal("10"),
        max_sector_pct=Decimal("8"), max_total_exposure_pct=Decimal("40"),
    )
    total_equity = Decimal("100000")
    stop_loss = Decimal("90")  # price=100 -> risk_per_share=10 (stands in for an ATR-derived stop)
    snapshot = _snapshot(total_equity=total_equity, holdings_value=Decimal("30000"), sector_exposure={"Tech": Decimal("3500")})
    buy = _buy(confidence=limits.min_confidence, stop_loss=stop_loss, requested_quantity=Decimal("1000"), sector="Tech")
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("4000"))

    decision = evaluate_risk(buy, snapshot, limits, opts)

    risk_per_share = Decimal("100") - stop_loss
    base_risk_budget = (limits.max_risk_per_trade_pct / 100) * total_equity
    adjusted_risk_budget = base_risk_budget * limits.min_position_size_multiplier
    max_position_notional = (limits.max_position_pct / 100) * total_equity
    remaining_sector = (limits.max_sector_pct / 100) * total_equity - Decimal("3500")
    remaining_exposure = (limits.max_total_exposure_pct / 100) * total_equity - Decimal("30000")
    cash_check = check_cash_sufficiency(Decimal("4000"), Decimal("0"), Decimal("0"))

    assert decision.approved
    assert Decimal("0") < decision.approved_quantity < Decimal("1000")
    final_notional = decision.approved_quantity * Decimal("100")

    # risk at stop never exceeds the confidence-adjusted budget, which never
    # exceeds the absolute max risk-per-trade budget
    assert decision.approved_quantity * risk_per_share <= adjusted_risk_budget
    assert adjusted_risk_budget <= base_risk_budget
    # final notional respects every capacity cap simultaneously
    assert final_notional <= max_position_notional
    assert final_notional <= remaining_sector
    assert final_notional <= remaining_exposure
    assert final_notional <= cash_check.max_affordable_notional
