from datetime import date
from decimal import Decimal

from tradepulse.strategy.options_selection import OptionContractSummary, select_contract

NOW = date(2026, 8, 26)


def _contract(days_out: int, strike: str, option_type: str = "call") -> OptionContractSummary:
    expiry = date.fromordinal(NOW.toordinal() + days_out)
    return OptionContractSummary(
        occ_symbol=f"AAPL{expiry.strftime('%y%m%d')}{'C' if option_type == 'call' else 'P'}{int(Decimal(strike) * 1000):08d}",
        underlying_symbol="AAPL",
        option_type=option_type,
        strike=Decimal(strike),
        expiry=expiry,
    )


def test_returns_none_on_empty_chain() -> None:
    assert select_contract("call", Decimal("150"), [], min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW) is None


def test_returns_none_when_nothing_survives_the_dte_window() -> None:
    chain = [_contract(5, "150"), _contract(90, "150")]
    assert select_contract("call", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW) is None


def test_returns_none_when_only_wrong_direction_is_eligible() -> None:
    chain = [_contract(30, "150", option_type="put")]
    assert select_contract("call", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW) is None


def test_picks_expiry_closest_to_window_midpoint() -> None:
    # window [21, 45] -> midpoint 33. 35 is closer to 33 than 22 is.
    near_midpoint = _contract(35, "150")
    far_from_midpoint = _contract(22, "150")
    chain = [far_from_midpoint, near_midpoint]

    result = select_contract("call", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW)

    assert result is near_midpoint


def test_picks_call_strike_above_spot_by_target_otm_pct() -> None:
    # spot=150, target_otm_pct=3 -> target strike 154.5 -- 155 is closer than 150 or 160.
    chain = [_contract(30, "150"), _contract(30, "155"), _contract(30, "160")]

    result = select_contract("call", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW)

    assert result.strike == Decimal("155")


def test_picks_put_strike_below_spot_by_target_otm_pct() -> None:
    # spot=150, target_otm_pct=3 -> target strike 145.5 -- 145 is closer than 140 or 150.
    chain = [_contract(30, "140", "put"), _contract(30, "145", "put"), _contract(30, "150", "put")]

    result = select_contract("put", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW)

    assert result.strike == Decimal("145")


def test_strike_selection_only_considers_the_chosen_expiry() -> None:
    """A strike closer to target on a DIFFERENT (non-chosen) expiry must
    never win -- expiry selection happens first, strike selection second,
    scoped to that expiry only."""
    chosen_expiry_contract = _contract(35, "160")  # 35 is the midpoint-closest expiry
    other_expiry_better_strike = _contract(22, "154.5")  # exact target strike, but the "wrong" expiry
    chain = [chosen_expiry_contract, other_expiry_better_strike]

    result = select_contract("call", Decimal("150"), chain, min_dte=21, max_dte=45, target_otm_pct=Decimal("3"), now=NOW)

    assert result is chosen_expiry_contract
