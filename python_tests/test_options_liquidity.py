"""Rev.113: option-specific execution limits and liquidity-aware contract choice.

Three soaks attempted about ten option buys; only one passed the shared 1.5%
spread limit (observed spreads: 1.87, 1.93, 2.58, 2.94, 3.79, 3.79, 5.80%).
Spread is measured relative to the premium, so a 5-cent market on a $3
contract is already 1.7% -- a limit calibrated for shares blocks options.
"""
from datetime import date
from decimal import Decimal

import pytest

from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, Side
from tradepulse.risk.engine import RiskCheckInput, RiskEvalOptions, evaluate_risk
from tradepulse.strategy.options_selection import (
    OptionContractSummary,
    choose_liquid_contract,
    option_candidates,
    select_contract,
    spread_pct,
)

TODAY = date(2026, 10, 1)
EXPIRY = date(2026, 11, 6)  # 36 DTE: the balanced window's midpoint expiry


def contract(strike, expiry=EXPIRY):
    return OptionContractSummary(f"IWM{expiry:%y%m%d}C{int(strike * 1000):08d}", "IWM", "call", Decimal(strike), expiry)


CHAIN = [contract(s) for s in ("280", "283", "285", "287", "288", "290", "295")] + [contract("287", date(2026, 10, 9))]


def candidates(count=5):
    # spot 279 x 1.03 = 287.37 -> nearest strikes 287, 288, 285, 290, 283
    return option_candidates("call", Decimal(279), CHAIN, min_dte=21, max_dte=45,
                             target_otm_pct=Decimal(3), now=TODAY, count=count)


def test_candidates_are_nearest_strikes_of_the_selected_expiry_and_lead_with_select_contract():
    found = candidates()
    assert [c.strike for c in found] == [Decimal(s) for s in ("287", "288", "285", "290", "283")]
    assert {c.expiry for c in found} == {EXPIRY}  # the 8-DTE contract is outside the window
    assert found[0] == select_contract("call", Decimal(279), CHAIN, min_dte=21, max_dte=45,
                                       target_otm_pct=Decimal(3), now=TODAY)


def test_no_eligible_expiry_yields_no_candidates():
    assert option_candidates("call", Decimal(279), [contract("287", date(2026, 10, 9))], min_dte=21, max_dte=45,
                             target_otm_pct=Decimal(3), now=TODAY) == []


def test_spread_pct_matches_risk_engine_formula():
    assert spread_pct(Decimal("2.88"), Decimal("2.99")) == (Decimal("0.11") / Decimal("2.935")) * 100


def test_choose_prefers_closest_to_target_within_limit():
    found = candidates()
    quoted = [(found[0], Decimal("2.88"), Decimal("2.99")),   # 3.75% -- target strike, too wide
              (found[1], Decimal("2.60"), Decimal("2.64")),   # 1.53% -- next nearest, acceptable
              (found[2], Decimal("3.50"), Decimal("3.52"))]   # 0.57% -- tighter but further from target
    chosen, bid, ask = choose_liquid_contract(quoted, Decimal("2.5"))
    assert chosen == found[1] and (bid, ask) == (Decimal("2.60"), Decimal("2.64"))


def test_choose_keeps_target_when_it_is_already_acceptable():
    found = candidates()
    quoted = [(found[0], Decimal("2.90"), Decimal("2.95")), (found[1], Decimal("2.60"), Decimal("2.61"))]
    assert choose_liquid_contract(quoted, Decimal("2.5"))[0] == found[0]


def test_choose_falls_back_to_tightest_so_the_risk_engine_records_the_rejection():
    found = candidates()
    quoted = [(found[0], Decimal("2.80"), Decimal("2.99")), (found[1], Decimal("2.50"), Decimal("2.60"))]
    assert choose_liquid_contract(quoted, Decimal("2.5"))[0] == found[1]
    assert choose_liquid_contract([], Decimal("2.5")) is None


def _option_buy():
    return RiskCheckInput(symbol="IWM261106C00287000", asset_class=AssetClass.OPTION, side=Side.BUY,
                          requested_quantity=Decimal(1), price=Decimal("2.935"), confidence=Decimal(90),
                          contract_multiplier=Decimal(100))


@pytest.mark.parametrize(("asset_class", "blocked"), [(AssetClass.OPTION, False), (AssetClass.EQUITY, True)])
def test_option_limits_apply_only_to_options(asset_class, blocked):
    from test_risk_engine import _snapshot

    limits = risk_limits_for_profile("balanced")
    intent = _option_buy() if asset_class == AssetClass.OPTION else RiskCheckInput(
        symbol="AAPL", asset_class=AssetClass.EQUITY, side=Side.BUY, requested_quantity=Decimal(1),
        price=Decimal("2.935"), confidence=Decimal(90))
    # 2.04% spread, 1.02% estimated slippage: inside the options limits, outside the share limits
    opts = RiskEvalOptions(bid=Decimal("2.905"), ask=Decimal("2.965"), estimated_slippage_pct=Decimal("1.02"),
                           available_cash=Decimal(100000))
    reasons = evaluate_risk(intent, _snapshot(), limits, opts).reasons
    assert any(r.startswith("SPREAD_EXCEEDS_LIMIT") for r in reasons) is blocked
    assert any(r.startswith("SLIPPAGE_EXCEEDS_LIMIT") for r in reasons) is blocked


def test_option_limits_still_reject_excessive_spread():
    from test_risk_engine import _snapshot

    opts = RiskEvalOptions(bid=Decimal("2.80"), ask=Decimal("2.99"), estimated_slippage_pct=Decimal("3.28"),
                           available_cash=Decimal(100000))
    reasons = evaluate_risk(_option_buy(), _snapshot(), risk_limits_for_profile("balanced"), opts).reasons
    assert "SPREAD_EXCEEDS_LIMIT (6.56% > 2.5%)" in reasons
    assert "SLIPPAGE_EXCEEDS_LIMIT (3.28% > 1.25%)" in reasons


@pytest.mark.parametrize("profile", ["aggressive", "balanced", "conservative", "micro"])
def test_every_profile_defines_wider_option_limits(profile):
    limits = risk_limits_for_profile(profile)
    assert limits.spread_limit_for(AssetClass.OPTION) > limits.spread_limit_for(AssetClass.EQUITY)
    assert limits.slippage_limit_for(AssetClass.OPTION) > limits.slippage_limit_for(AssetClass.EQUITY)
    assert limits.spread_limit_for(AssetClass.CRYPTO) == limits.spread_limit_pct
    assert limits.slippage_limit_for(AssetClass.OPTION) * 2 == limits.spread_limit_for(AssetClass.OPTION)
