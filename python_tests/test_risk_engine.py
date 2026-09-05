import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.config import risk_limits_for_profile
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    Holding,
    PnlRecord,
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


def test_regime_multiplier_composes_with_confidence_multiplicatively() -> None:
    """Market Regime Phase 2: regime_multiplier scales the SAME risk_budget
    confidence already scaled, immediately after it -- the combined effect
    must be the exact product of both fractions, not either one alone."""
    stop_loss = Decimal("96")  # risk_per_share = 4 against price=100
    total_equity = Decimal("100000")
    base_risk_budget = (LIMITS.max_risk_per_trade_pct / 100) * total_equity
    confidence_multiplier = LIMITS.min_position_size_multiplier  # confidence == min_confidence -> the floor
    regime_multiplier = Decimal("0.5")
    combined_budget = base_risk_budget * confidence_multiplier * regime_multiplier

    buy = _buy(
        confidence=LIMITS.min_confidence, stop_loss=stop_loss, requested_quantity=Decimal("300"),
        regime_multiplier=regime_multiplier,
    )
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(total_equity=total_equity), LIMITS, opts)

    risk_per_share = Decimal("100") - stop_loss
    assert decision.approved
    assert decision.approved_quantity * risk_per_share <= combined_budget
    assert combined_budget < base_risk_budget * confidence_multiplier  # regime genuinely shrank it further, not a no-op
    assert any("RISK_BUDGET_SCALED_BY_CONFIDENCE" in r for r in decision.reasons)
    assert any("RISK_BUDGET_SCALED_BY_REGIME_TO_50.0PCT" in r for r in decision.reasons)


def test_regime_multiplier_at_or_above_one_never_scales_risk_budget_up() -> None:
    stop_loss = Decimal("96")
    total_equity = Decimal("100000")
    base_risk_budget = (LIMITS.max_risk_per_trade_pct / 100) * total_equity
    buy = _buy(confidence=Decimal("100"), stop_loss=stop_loss, requested_quantity=Decimal("300"), regime_multiplier=Decimal("1"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(total_equity=total_equity), LIMITS, opts)

    risk_per_share = Decimal("100") - stop_loss
    assert decision.approved
    assert decision.approved_quantity * risk_per_share <= base_risk_budget
    assert not any("RISK_BUDGET_SCALED_BY_REGIME" in r for r in decision.reasons)  # >= 1 never triggers the scaling branch at all


def test_regime_multiplier_zero_produces_insufficient_capacity_rejection() -> None:
    """The liquidity_crisis path (position_multiplier=0), reusing the
    EXISTING deterministic rejection semantics -- no new kill-switch/
    session-state machinery, confirmed directly here without needing the
    classifier itself."""
    buy = _buy(confidence=Decimal("100"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"), regime_multiplier=Decimal("0"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    decision = evaluate_risk(buy, _snapshot(total_equity=Decimal("100000")), LIMITS, opts)

    assert not decision.approved
    assert decision.approved_quantity == Decimal("0")
    assert any("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT" in r for r in decision.reasons)


def test_regime_cannot_push_approved_quantity_above_existing_hard_caps() -> None:
    """regime_multiplier=1 (no risk-budget scaling at all) still lands on
    the SAME max_position_pct-capped quantity as with no regime signal --
    proving the existing hard caps remain unconditionally enforced
    regardless of what regime did or didn't do to the risk budget."""
    limits = replace(LIMITS, max_risk_per_trade_pct=Decimal("50"))  # deliberately huge so max_position_pct binds instead
    buy_with_regime = _buy(confidence=Decimal("100"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"), regime_multiplier=Decimal("1"))
    buy_without_regime = _buy(confidence=Decimal("100"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    snapshot = _snapshot(total_equity=Decimal("100000"))

    with_regime = evaluate_risk(buy_with_regime, snapshot, limits, opts)
    without_regime = evaluate_risk(buy_without_regime, snapshot, limits, opts)

    assert with_regime.approved and without_regime.approved
    assert with_regime.approved_quantity == without_regime.approved_quantity
    assert any("BY_MAX_POSITION_PCT" in r for r in with_regime.reasons)


def test_regime_multiplier_none_behaves_identically_to_no_regime_signal() -> None:
    """Full backward compatibility -- every existing caller/test that never
    passes regime_multiplier (the default, None) must be completely
    unaffected. Explicit regression test, not just "every other test in
    this file still passes"."""
    buy_explicit_none = _buy(confidence=Decimal("90"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"), regime_multiplier=None)
    buy_omitted = _buy(confidence=Decimal("90"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    snapshot = _snapshot(total_equity=Decimal("100000"))

    a = evaluate_risk(buy_explicit_none, snapshot, LIMITS, opts)
    b = evaluate_risk(buy_omitted, snapshot, LIMITS, opts)
    assert a == b


def test_invalid_regime_multiplier_rejects_the_whole_intent_never_ignored_or_clamped() -> None:
    """Defense-in-depth at the deterministic risk boundary: the risk engine
    validates regime_multiplier itself rather than trusting the caller.
    Each case is a genuinely invalid signal (out of [0, 1], or non-finite)
    that should never reach this module in normal operation -- proving it
    is rejected outright, not silently skipped or clamped into range."""
    for bad_multiplier in (Decimal("-0.5"), Decimal("1.25"), Decimal("NaN"), Decimal("Infinity")):
        buy = _buy(confidence=Decimal("90"), stop_loss=Decimal("96"), requested_quantity=Decimal("300"), regime_multiplier=bad_multiplier)
        opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
        decision = evaluate_risk(buy, _snapshot(total_equity=Decimal("100000")), LIMITS, opts)

        assert not decision.approved, f"expected rejection for regime_multiplier={bad_multiplier}"
        assert decision.approved_quantity == Decimal("0")
        assert any("INVALID_REGIME_MULTIPLIER" in r for r in decision.reasons), f"missing reason for regime_multiplier={bad_multiplier}: {decision.reasons}"


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


async def test_max_drawdown_finds_a_real_peak_buried_behind_a_large_lower_history(tmp_path) -> None:
    """Rev.83 RISK-002: check_max_drawdown used to scan list_all(limit=1000)
    -- oldest-first -- so a genuine peak recorded AFTER 1000 older, lower
    snapshots existed could fall outside that window and be missed
    entirely, understating drawdown. Now max_by_json_field does one
    full-table SQL aggregation, so the real peak is found regardless of
    how much lower history exists."""
    repositories = await _repositories(tmp_path)
    await asyncio.gather(*(_seed_equity_snapshot(repositories, Decimal("50000"), f"old-snap-{i}") for i in range(1100)))
    await _seed_equity_snapshot(repositories, Decimal("120000"), "real-peak")  # the genuine, higher, newer peak
    limits = replace(LIMITS, max_drawdown_pct=Decimal("15"))

    check = await check_max_drawdown(repositories, Decimal("105000"), limits)

    assert check.peak_equity == Decimal("120000")
    assert check.drawdown_pct == Decimal("12.5")
    assert check.breached is False  # 12.5% < 15% limit -- would have wrongly read as 0% (or breached the wrong way) against the old $50000 "peak"


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


async def test_build_portfolio_snapshot_includes_a_holding_beyond_one_page_of_others(tmp_path) -> None:
    """FIN-090-01 Tier 2 -- the most important single regression in this
    fix: the old list_all() default (limit=1000, oldest-first) would
    silently drop a holding once enough OTHER holdings existed first,
    UNDERSTATING holdings_value/sector_exposure and therefore making
    max_total_exposure_pct/max_sector_pct limit checks MORE PERMISSIVE
    than they should be -- the exact wrong direction for a risk control."""
    repositories = await _repositories(tmp_path)

    async def _noise(i: int) -> None:
        noise_asset = AssetIdentity(f"NOISE{i}", AssetClass.EQUITY, f"alpaca:NOISE{i}")
        holding = Holding(asset=noise_asset, quantity=Decimal("1"), average_price=Decimal("10"), updated_at=NOW, sector="Noise")
        await repositories.holdings.create_once(asset_identity_key(noise_asset), holding)

    await asyncio.gather(*(_noise(i) for i in range(501)))  # more than one default page (500)
    holding = Holding(asset=_aapl(), quantity=Decimal("10"), average_price=Decimal("100"), updated_at=NOW, sector="Tech")
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.holdings_value == Decimal("1000") + Decimal("501") * Decimal("10")  # AAPL's 1000 must be included, not silently dropped
    assert snapshot.sector_exposure["Tech"] == Decimal("1000")  # AAPL's own sector exposure specifically must be present


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


async def _seed_fill(repositories: PersistenceRepositories, *, fill_id: str, trade_intent_id: str, quantity: Decimal, filled_at: datetime) -> None:
    fill = Fill(
        fill_id, trade_intent_id, f"order-{fill_id}", _aapl(), Side.BUY, ExecutionMode.PAPER,
        quantity, Decimal("100"), Decimal("0"), Decimal("0"), filled_at,
    )
    await repositories.fills.create_once(fill_id, fill, unique_value=None)


async def test_trades_today_counts_distinct_trades_not_fill_rows(tmp_path) -> None:
    """Rev.81 Finding 3: one order that fills in several partial broker
    fills (confirmed live -- one real order filled in 5 pieces) must count
    ONCE against max_daily_trades, not once per Fill row."""
    repositories = await _repositories(tmp_path)
    for i, qty in enumerate(["7", "3", "2", "1", "0.195"]):
        await _seed_fill(repositories, fill_id=f"fill-{i}", trade_intent_id="ti-same-order", quantity=Decimal(qty), filled_at=NOW)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 1


async def test_trades_today_counts_each_distinct_trade_intent(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_fill(repositories, fill_id="fill-1", trade_intent_id="ti-a", quantity=Decimal("5"), filled_at=NOW)
    await _seed_fill(repositories, fill_id="fill-2", trade_intent_id="ti-b", quantity=Decimal("5"), filled_at=NOW)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 2


async def test_trades_today_excludes_fills_from_a_prior_day(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    yesterday = NOW.replace(day=NOW.day - 1)
    await _seed_fill(repositories, fill_id="fill-1", trade_intent_id="ti-old", quantity=Decimal("5"), filled_at=yesterday)
    await _seed_fill(repositories, fill_id="fill-2", trade_intent_id="ti-new", quantity=Decimal("5"), filled_at=NOW)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 1


async def test_risk_day_boundary_is_exact_at_ny_midnight(tmp_path) -> None:
    """NOW (2026-08-15T00:00:00+00:00) is 2026-08-14T20:00 EDT -- inside NY
    risk-day Aug 14, which runs [2026-08-14T04:00 UTC, 2026-08-15T04:00 UTC).
    A fill one second before that boundary counts; one exactly at/after it
    does not."""
    repositories = await _repositories(tmp_path)
    just_before = datetime(2026, 8, 15, 3, 59, 59, tzinfo=UTC)
    at_boundary = datetime(2026, 8, 15, 4, 0, 0, tzinfo=UTC)
    await _seed_fill(repositories, fill_id="fill-in", trade_intent_id="ti-in", quantity=Decimal("5"), filled_at=just_before)
    await _seed_fill(repositories, fill_id="fill-out", trade_intent_id="ti-out", quantity=Decimal("5"), filled_at=at_boundary)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 1


async def test_risk_day_boundary_resolves_utc_vs_ny_session_mismatch(tmp_path) -> None:
    """Rev.83 RISK-003: the risk day is now NY-midnight-to-NY-midnight, not
    a UTC calendar day. A fill at 2026-08-14T12:00 UTC has UTC date()
    2026-08-14, different from NOW's UTC date() (2026-08-15) -- the OLD
    `f.filled_at.date() == now.date()` comparison would have wrongly
    EXCLUDED it, even though it falls squarely inside the same NY trading
    session as NOW (NY-day Aug 14 runs 04:00 UTC Aug 14 -> 04:00 UTC
    Aug 15). Proves this is a real behavior change, not just a rename."""
    repositories = await _repositories(tmp_path)
    same_ny_day_different_utc_date = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    await _seed_fill(repositories, fill_id="fill-1", trade_intent_id="ti-1", quantity=Decimal("5"), filled_at=same_ny_day_different_utc_date)

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 1  # old UTC-date comparison would have given 0


async def test_trades_today_counts_one_trade_despite_a_large_partial_fill_backlog_in_the_window(tmp_path) -> None:
    """A single TradeIntent with far more than 1000 partial Fill rows
    inside one risk day still counts as exactly one trade -- both because
    trades_today counts distinct trade_intent_id (Rev.81 fix) AND because
    the fetch itself no longer truncates at any row count (Rev.83 fix)."""
    repositories = await _repositories(tmp_path)

    async def _seed(i: int) -> None:
        await _seed_fill(repositories, fill_id=f"fill-{i}", trade_intent_id="ti-1", quantity=Decimal("0.001"), filled_at=NOW)

    await asyncio.gather(*(_seed(i) for i in range(1100)))

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), now=NOW)

    assert snapshot.trades_today == 1


async def _seed_pnl(repositories: PersistenceRepositories, *, record_id: str, realized: Decimal, as_of: datetime) -> None:
    record = PnlRecord(record_id, _aapl(), realized, Decimal("0"), as_of)
    await repositories.pnl_records.create_once(record_id, record)


async def test_daily_realized_sums_exactly_over_a_large_backlog_in_the_window(tmp_path) -> None:
    """Rev.83: daily_realized's fetch is no longer list_all(limit=1000) --
    proves a window containing far more than 1000 PnL records is summed
    completely, and the sum stays an exact Decimal (never a lossy float
    round-trip through SQL aggregation)."""
    repositories = await _repositories(tmp_path)

    async def _seed(i: int) -> None:
        await _seed_pnl(repositories, record_id=f"pnl-{i}", realized=Decimal("0.01"), as_of=NOW)

    await asyncio.gather(*(_seed(i) for i in range(1100)))

    snapshot = await build_portfolio_snapshot(repositories, cash_balance=Decimal("0"), account_equity=Decimal("100000"), now=NOW)

    assert snapshot.daily_pnl_pct == (Decimal("1100") * Decimal("0.01") / Decimal("100000")) * 100


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


def test_options_contract_multiplier_scales_notional_caps_100x_versus_equivalent_equity() -> None:
    """A 'known-good ratio' proof the multiplier is applied at all: an
    options position and an equity position with IDENTICAL per-unit price/
    stop/quantity math, differing only in contract_multiplier=100 vs 1,
    must be sized so the option's real dollar notional is exactly 100x the
    equity's for the same requested_quantity, both landing on the same
    max_position_pct cap."""
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("10000000"))
    snapshot = _snapshot(total_equity=Decimal("1000000"))
    limits = replace(LIMITS, max_position_pct=Decimal("50"), max_sector_pct=Decimal("90"), max_total_exposure_pct=Decimal("95"))

    equity_decision = evaluate_risk(
        _buy(asset_class=AssetClass.EQUITY, requested_quantity=Decimal("100000"), price=Decimal("100"), confidence=Decimal("100")),
        snapshot, limits, opts,
    )
    option_decision = evaluate_risk(
        _buy(asset_class=AssetClass.OPTION, requested_quantity=Decimal("100000"), price=Decimal("100"), confidence=Decimal("100"), contract_multiplier=Decimal("100")),
        snapshot, limits, opts,
    )

    assert equity_decision.approved and option_decision.approved
    equity_notional = equity_decision.approved_quantity * Decimal("100")
    option_notional = option_decision.approved_quantity * Decimal("100") * Decimal("100")  # price * multiplier
    # Both hit the SAME max_position_pct cap ($500,000 = 50% of $1,000,000)
    # -- the option's approved_quantity is naturally 100x smaller (contracts,
    # not shares) so the two notionals land on the identical dollar cap.
    assert equity_notional == Decimal("500000")
    assert option_notional == Decimal("500000")
    assert option_decision.approved_quantity * 100 == equity_decision.approved_quantity


def test_risk_based_sizing_applies_multiplier_before_dividing_never_only_to_downstream_caps() -> None:
    """Regression test for the exact defect flagged in plan review: applying
    contract_multiplier only to the downstream notional caps (not to
    unit_risk before the division that derives risk_based_qty) would
    transiently size ~100x too large at the risk-based-sizing step itself.
    Here every OTHER cap is deliberately set generous enough that it would
    NOT be the binding constraint for the CORRECT answer (10 contracts) but
    WOULD still catch and reduce the wrong answer (1000 contracts) down to a
    different, distinguishable value (250) -- proving the two formulas are
    not just theoretically different but produce different observable
    approved_quantity values, and that this implementation lands on the
    correct one."""
    limits = replace(LIMITS, max_risk_per_trade_pct=Decimal("1.0"), max_position_pct=Decimal("50"), max_sector_pct=Decimal("50"), max_total_exposure_pct=Decimal("90"))
    snapshot = _snapshot(total_equity=Decimal("100000"))
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("1000000"))
    # risk_budget = 1% * $100,000 = $1,000 (confidence=100 avoids any
    # confidence-multiplier scaling so this stays exact).
    # CORRECT: unit_risk = (2.00 - 1.00) * 100 = 100 -> qty = 1000/100 = 10.
    # WRONG (multiplier applied only downstream): qty = 1000/1.00 = 1000,
    # which max_position_pct (50% * $100,000 = $50,000 / (2.00*100) = 250)
    # would then catch and reduce to 250 -- a different, wrong answer.
    buy = _buy(
        asset_class=AssetClass.OPTION, requested_quantity=Decimal("2000"), price=Decimal("2.00"),
        stop_loss=Decimal("1.00"), confidence=Decimal("100"), contract_multiplier=Decimal("100"),
    )

    decision = evaluate_risk(buy, snapshot, limits, opts)

    assert decision.approved
    assert decision.approved_quantity == Decimal("10")
    assert any("POSITION_CAPPED_TO_10_BY_RISK_BASED_SIZING" in r for r in decision.reasons)
    assert not any("BY_MAX_POSITION_PCT" in r for r in decision.reasons)  # never reached -- risk-based sizing already won


def test_options_decision_satisfies_both_stop_based_risk_and_premium_commitment_invariants() -> None:
    """The two invariants from plan review, asserted SEPARATELY so they can
    never be silently conflated: stop-based risk answers 'how much can this
    lose if the stop is honored', premium commitment answers 'how much is
    actually spent up front' -- for a long option the max possible loss is
    the whole premium, not just the stop-to-entry distance, so a position
    could pass one check while still failing the other if either were
    missing multiplier-awareness."""
    limits = replace(LIMITS, max_risk_per_trade_pct=Decimal("0.5"), max_position_pct=Decimal("10"), max_sector_pct=Decimal("50"), max_total_exposure_pct=Decimal("50"))
    total_equity = Decimal("100000")
    snapshot = _snapshot(total_equity=total_equity)
    opts = RiskEvalOptions(skip_market_data_checks=True, available_cash=Decimal("50000"))
    price = Decimal("2.00")
    stop_loss = Decimal("1.30")
    multiplier = Decimal("100")
    buy = _buy(
        asset_class=AssetClass.OPTION, requested_quantity=Decimal("500"), price=price,
        stop_loss=stop_loss, confidence=limits.min_confidence, contract_multiplier=multiplier,
    )

    decision = evaluate_risk(buy, snapshot, limits, opts)

    assert decision.approved
    assert decision.approved_quantity > 0
    confidence_multiplier = limits.min_position_size_multiplier  # confidence pinned at exactly min_confidence
    adjusted_risk_budget = (limits.max_risk_per_trade_pct / 100) * total_equity * confidence_multiplier

    # Invariant 1 -- stop-based risk never exceeds the confidence-adjusted budget.
    stop_based_risk = decision.approved_quantity * abs(price - stop_loss) * multiplier
    assert stop_based_risk <= adjusted_risk_budget

    # Invariant 2 -- total premium committed never exceeds available cash
    # or remaining exposure/sector/position capacity, independent of
    # whatever the stop-based risk figure was.
    premium_committed = decision.approved_quantity * price * multiplier
    assert premium_committed <= opts.available_cash
    assert premium_committed <= (limits.max_position_pct / 100) * total_equity
    assert premium_committed <= (limits.max_sector_pct / 100) * total_equity
    assert premium_committed <= (limits.max_total_exposure_pct / 100) * total_equity
