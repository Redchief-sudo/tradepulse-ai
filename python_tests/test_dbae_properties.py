"""DBAE property tests (spec §14 item 10; plan Task 10).

Over random balances (0 to 5,000), prices, asset minimums and increments,
and pending sets:

1. no approval when any field the asset class requires is None, and a
   missing field the class does not require never blocks it;
2. the shaped quantity is never above the risk-approved quantity;
3. at every approval, the new buy's max_funding is within the funding
   available, the pending figure it was judged against is exactly what the
   earlier approvals still reserve, and so the total reserved stays within
   the account's funding;
4. a protective exit never carries a DBAE reason, whatever the balance and
   whatever the DBAE evidence.

Properties 1 and 2 run against the pure policy; 3 and 4 drive the
execution gateway on the simulated broker (no broker or network calls).
Hypothesis runs derandomized with no example database, so CI sees the same
examples every run.
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import NamedTuple

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from test_dbae_gateway import (
    FRAC,
    FRAY,
    FRAZ,
    NOW,
    OPT,
    SOL,
    WHLE,
    _account,
    _assets,
    _buy,
    _dbae,
    _events,
    _held,
    _hold_orders_open,
    _rig,
)

from tradepulse.broker.capabilities import AccountEvidence, AssetTradingCapabilities
from tradepulse.config import risk_limits_for_profile
from tradepulse.dbae.bands import Band, band_for, unlocks
from tradepulse.dbae.funding import fundable_notional, funding_available, max_funding
from tradepulse.dbae.policy import EntryProposal, evaluate_entry_policy, shape_quantity
from tradepulse.execution import ExecutionResult
from tradepulse.models import AssetClass, AssetIdentity, TradeIntentStatus

pytestmark = pytest.mark.usefixtures("open_market_calendar")

# Three function-scoped fixtures are shared by every example of a test:
# open_market_calendar (a fixed calendar), conftest's autouse held_lease (one
# lease directory and the held TEST_ACCOUNT lease that every example's
# gateway carries) and conftest's autouse clock-calendar disagreement MONITOR
# reset. The first two are the same for every example. MONITOR is process
# state that the gateway's session decision observes, so each example resets
# it itself (_fresh_example) instead of relying on the per-test reset. Every
# example builds its own database, broker and clock.
_SHARED_FIXTURES = [HealthCheck.function_scoped_fixture]
PURE = settings(max_examples=400, derandomize=True, database=None, deadline=None)
FUNDING = settings(max_examples=50, derandomize=True, database=None, deadline=None,
                   suppress_health_check=_SHARED_FIXTURES)
EXITS = settings(max_examples=20, derandomize=True, database=None, deadline=None,
                 suppress_health_check=_SHARED_FIXTURES)

# Widest slippage allowance first: hypothesis tries earlier elements more often.
PROFILES = ("aggressive", "balanced", "micro", "conservative")
BALANCE = st.decimals(min_value=Decimal(0), max_value=Decimal(5000), places=2, allow_nan=False, allow_infinity=False)
# An account holding a position has a funding history: the simulator's opening journal needs a deposit.
HOLDING_BALANCE = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(5000), places=2, allow_nan=False,
                              allow_infinity=False)
FUNDED = st.decimals(min_value=Decimal(20), max_value=Decimal(5000), places=2, allow_nan=False, allow_infinity=False)
PRICE = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(2000), places=2, allow_nan=False,
                    allow_infinity=False)
QUANTITY = st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal(1000), places=8, allow_nan=False,
                       allow_infinity=False)
INCREMENT = st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal(1), places=8, allow_nan=False,
                        allow_infinity=False)
# A large request leaves the caps to size the buy, as a strategy's request usually does.
REQUEST = st.one_of(st.just(Decimal(1000)), QUANTITY)
# Small shares of the balance make the funding cap bind, where the reservation is tightest.
FRACTION = st.one_of(st.sampled_from([Decimal("0.01"), Decimal("0.02"), Decimal("0.05"), Decimal("0.1")]),
                     st.decimals(min_value=Decimal(0), max_value=Decimal(1), places=2, allow_nan=False,
                                 allow_infinity=False))
STOP_PCT = st.one_of(st.sampled_from([Decimal(1), Decimal(2), Decimal(6)]),
                     st.decimals(min_value=Decimal("0.5"), max_value=Decimal(30), places=1, allow_nan=False,
                                 allow_infinity=False))

ACCOUNT_FIELDS = ("status", "trading_blocked", "account_blocked", "trade_suspended_by_user", "suspend_trade",
                  "closing_transactions_only", "fractional_trading", "equity", "cash", "buying_power",
                  "non_marginable_buying_power", "account_multiplier", "crypto_status", "options_trading_level")
ASSET_FIELDS = ("active", "tradable", "fractionable", "min_order_size", "min_trade_increment", "price_increment")
ALL_FIELDS = tuple(f"account.{name}" for name in ACCOUNT_FIELDS) + tuple(f"asset.{name}" for name in ASSET_FIELDS)
_CORE = frozenset({"account.status", "account.trading_blocked", "account.account_blocked",
                   "account.trade_suspended_by_user", "account.suspend_trade", "account.closing_transactions_only",
                   "account.account_multiplier", "account.equity", "account.cash", "account.buying_power",
                   "asset.active", "asset.tradable"})
_MICRO_BANDS = frozenset({Band.MICRO, Band.MICRO_PLUS})
_ASSETS = {AssetClass.EQUITY: FRAC, AssetClass.CRYPTO: SOL, AssetClass.OPTION: OPT}


def required_fields(asset_class: AssetClass, band: Band) -> frozenset[str]:
    """Spec §5.2 and §6: the evidence an entry of this class needs in this band."""
    if asset_class is AssetClass.EQUITY:
        extra = {"account.fractional_trading", "asset.fractionable"} if band in _MICRO_BANDS else set()
    elif asset_class is AssetClass.CRYPTO:
        extra = {"account.crypto_status", "account.non_marginable_buying_power", "asset.min_order_size",
                 "asset.min_trade_increment"}
    else:
        extra = {"account.options_trading_level"}
    return _CORE | extra


def class_unlocked(asset_class: AssetClass, band: Band) -> bool:
    band_unlocks = unlocks(band)
    if asset_class is AssetClass.EQUITY:
        return band_unlocks.equity != "none"
    if asset_class is AssetClass.CRYPTO:
        return band_unlocks.crypto
    return band_unlocks.options


def _knock_out(account: AccountEvidence, asset: AssetTradingCapabilities,
               missing: frozenset[str]) -> tuple[AccountEvidence, AssetTradingCapabilities]:
    account_none = {name.removeprefix("account."): None for name in missing if name.startswith("account.")}
    asset_none = {name.removeprefix("asset."): None for name in missing if name.startswith("asset.")}
    return replace(account, **account_none), replace(asset, **asset_none)


def _verified_asset(minimum: Decimal, increment: Decimal) -> AssetTradingCapabilities:
    return AssetTradingCapabilities(True, True, True, minimum, increment, Decimal("0.01"), NOW)


# --- 1. no approval when a required field is None (spec §5, §6, §10) ---------------------------------------


@PURE
@given(
    balance=FUNDED,
    asset_class=st.sampled_from([AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION]),
    missing=st.frozensets(st.sampled_from(ALL_FIELDS), max_size=4),
    baseline_known=st.booleans(),
    minimum=INCREMENT,
    increment=INCREMENT,
    price=PRICE,
)
def test_no_approval_with_a_required_field_missing(balance: Decimal, asset_class: AssetClass, missing: frozenset[str],
                                                   baseline_known: bool, minimum: Decimal, increment: Decimal,
                                                   price: Decimal) -> None:
    """Starting from fully verified evidence, the fields in ``missing`` are
    None. The entry is approved exactly when none of them is required for
    its class and band, the band unlocks the class and the baseline is
    known: a missing field never approves, and a field another class needs
    never blocks this one."""
    account, asset = _knock_out(_account(balance), _verified_asset(minimum, increment), missing)
    band_equity = balance if baseline_known else None
    proposal = EntryProposal(_ASSETS[asset_class], price, price * Decimal("0.9"), "alpaca-paper", NOW)

    policy = evaluate_entry_policy(account, band_equity, asset, proposal)

    band = band_for(balance)
    blocked_by = missing & required_fields(asset_class, band)
    expected = not blocked_by and baseline_known and class_unlocked(asset_class, band)
    assert policy.allowed is expected, (sorted(missing), policy.reasons)
    if not policy.allowed:
        assert policy.reasons and all(reason.startswith("DBAE_") for reason in policy.reasons)

    # §7.5: funding is unknown, never permissive, when its inputs are missing.
    power = "account.non_marginable_buying_power" if asset_class is AssetClass.CRYPTO else "account.buying_power"
    available = funding_available(account, asset_class, Decimal(0))
    assert (available is None) is bool(missing & {"account.cash", power})


# --- 2. shaped <= risk-approved (spec §8.2) ----------------------------------------------------------------


@PURE
@given(
    balance=FUNDED,
    asset_class=st.sampled_from([AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.OPTION]),
    fractionable=st.booleans(),
    fractional_trading=st.booleans(),
    minimum=INCREMENT,
    increment=INCREMENT,
    approved=QUANTITY,
)
def test_shaped_never_exceeds_approved(balance: Decimal, asset_class: AssetClass, fractionable: bool,
                                       fractional_trading: bool, minimum: Decimal, increment: Decimal,
                                       approved: Decimal) -> None:
    account = replace(_account(balance), fractional_trading=fractional_trading)
    asset = replace(_verified_asset(minimum, increment), fractionable=fractionable)
    proposal = EntryProposal(_ASSETS[asset_class], Decimal(10), Decimal(9), "alpaca-paper", NOW)
    policy = evaluate_entry_policy(account, balance, asset, proposal)
    if not policy.allowed:
        return

    shaped = shape_quantity(policy, asset, asset_class, approved)

    assert Decimal(0) <= shaped.quantity <= approved
    if shaped.refusal is None:
        assert shaped.quantity > 0
        if asset_class is AssetClass.CRYPTO:
            assert shaped.quantity % increment == 0 and shaped.quantity >= minimum
        elif shaped.rule == "whole_shares":
            assert shaped.quantity == shaped.quantity.to_integral_value()
        else:
            assert shaped.quantity == approved


# --- 3. funding across a pending set (spec §7) -------------------------------------------------------------


@PURE
@given(
    available=st.decimals(min_value=Decimal("0.01"), max_value=Decimal(5000), places=2, allow_nan=False,
                          allow_infinity=False),
    asset=st.sampled_from([FRAC, SOL, OPT]),
    profile=st.sampled_from(PROFILES),
    price=PRICE,
)
def test_a_buy_sized_within_fundable_notional_reserves_within_funding(available: Decimal, asset: AssetIdentity,
                                                                      profile: str, price: Decimal) -> None:
    """The engine's cash cap (1% buffer) applied to fundable_notional bounds
    max_funding, slippage allowance and fee included, by the funding."""
    slippage = risk_limits_for_profile(profile).slippage_limit_for(asset.asset_class)
    per_unit = price * (100 if asset is OPT else 1)
    quantity = (fundable_notional(available, asset.asset_class, slippage) * Decimal("0.99") / per_unit).quantize(
        Decimal("0.00000001"), rounding=ROUND_FLOOR)
    assert max_funding(quantity, price, asset, slippage) <= available

_BUYABLE = (FRAC, FRAY, FRAZ, WHLE, SOL, OPT)


def _fresh_example() -> None:
    """Per-example reset of the process state a gateway observes (see _SHARED_FIXTURES)."""
    from tradepulse.calendar.disagreement import MONITOR

    MONITOR.reset()


class FundingCase(NamedTuple):
    balance: Decimal
    cash_share: Decimal
    power_share: Decimal
    profile: str
    crypto_minimum: Decimal
    crypto_increment: Decimal
    entries: list[tuple[AssetIdentity, Decimal, Decimal, Decimal]]


def _one_cent_stock(balance: str) -> FundingCase:
    """An aggressive account whose funding is 1% of ``balance``, buying a 1-cent stock with a 1% stop."""
    return FundingCase(Decimal(balance), Decimal("0.01"), Decimal("0.01"), "aggressive", Decimal("1E-8"),
                       Decimal("1E-8"), [(FRAC, Decimal("0.01"), Decimal(1000), Decimal(1))])


# Pinned for the funding property, each with the outcome it must still reach
# (test_pinned_funding_cases_keep_their_outcome), so none can pass vacuously.
# All three over-reserved before the fundable_notional fix.
PINNED_FUNDING_CASES = {
    # $50 of funding, a $10 stock: approved, reserving within $50.
    "funding_cap_binds": FundingCase(Decimal(1000), Decimal("0.05"), Decimal("0.05"), "aggressive",
                                     Decimal("0.001"), Decimal("0.001"), [(FRAC, Decimal(10), Decimal(1000), Decimal(1))]),
    # The counterexample hypothesis found unaided (100 examples, before the fix): $1.03 of funding. After the
    # fix the buy is under $1 at its stop, so it is refused, not approved over its funding.
    "one_cent_stock": _one_cent_stock("102.51"),
    # The same buy with $1.05 of funding: the tightest pinned approval (pre-fix max_funding $1.0551 > $1.05).
    "one_cent_stock_approved": _one_cent_stock("105"),
}
PINNED_OUTCOMES: dict[str, list[tuple[str, list[str]]]] = {
    "funding_cap_binds": [("pending", [])],
    "one_cent_stock": [("rejected", ["DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED"])],
    "one_cent_stock_approved": [("pending", [])],
}


@st.composite
def entry_sequences(draw: st.DrawFn) -> list[tuple[AssetIdentity, Decimal, Decimal, Decimal]]:
    """One to four buys on distinct symbols: (asset, price, requested quantity, stop %)."""
    assets = draw(st.lists(st.sampled_from(_BUYABLE), min_size=1, max_size=4, unique_by=lambda asset: asset.symbol))
    return [(asset, draw(PRICE), draw(REQUEST), draw(STOP_PCT)) for asset in assets]


async def _check_funding(balance: Decimal, cash_share: Decimal, power_share: Decimal, profile: str,
                         crypto_minimum: Decimal, crypto_increment: Decimal,
                         entries: list[tuple[AssetIdentity, Decimal, Decimal, Decimal]]) -> list[ExecutionResult]:
    """Checks every entry; returns each entry's result. A refused entry is
    only checked not to be pending."""
    _fresh_example()
    results: list[ExecutionResult] = []
    with tempfile.TemporaryDirectory() as directory:
        rig = await _rig(Path(directory), balance=balance, profile=profile)
        cash = (balance * cash_share).quantize(Decimal("0.01"))
        power = (balance * power_share).quantize(Decimal("0.01"))
        rig.caps.account = _account(balance, cash=cash, buying_power=power, non_marginable_buying_power=power)
        rig.caps.assets = _assets()
        rig.caps.assets[SOL.symbol] = replace(rig.caps.assets[SOL.symbol], min_order_size=crypto_minimum,
                                              min_trade_increment=crypto_increment)
        _hold_orders_open(rig)
        funding = min(cash, power)
        committed = Decimal(0)
        for asset, price, requested, stop_pct in entries:
            rig.quote(asset, price)
            pending_before = await rig.pending()
            assert pending_before == committed  # orders stay open: every approval is still reserved

            result = await rig.gateway.execute_intent(_buy(asset, requested, price * (1 - stop_pct / 100)))
            results.append(result)

            intent = next((i for i in await rig.intents() if i.trade_intent_id == result.trade_intent_id), None)
            if intent is None or "dbae" not in intent.risk_snapshot:
                assert result.status != "pending", result.reasons
                continue
            assert result.status == "pending", result.reasons
            assert intent.status == TradeIntentStatus.ACCEPTED
            dbae = _dbae(intent)
            submitted = Decimal(dbae["submitted_quantity"])
            reserved = Decimal(dbae["max_funding"])
            available = Decimal(dbae["funding_available"])
            assert Decimal(dbae["shaped_quantity"]) == submitted <= Decimal(dbae["risk_approved_quantity"])
            assert intent.requested_quantity == submitted
            assert Decimal(dbae["pending_buy_funding"]) == pending_before
            assert available == funding - pending_before
            assert reserved == max_funding(submitted, price, asset, rig.limits.slippage_limit_for(asset.asset_class))
            assert reserved <= available, (asset.symbol, profile, submitted, price, reserved, available)
            committed += reserved
            assert committed <= funding
        assert await rig.pending() == committed <= max(funding, Decimal(0))
    return results


@FUNDING
@given(
    balance=BALANCE,
    cash_share=FRACTION,
    power_share=FRACTION,
    profile=st.sampled_from(PROFILES),
    crypto_minimum=INCREMENT,
    crypto_increment=INCREMENT,
    entries=entry_sequences(),
)
@example(**PINNED_FUNDING_CASES["funding_cap_binds"]._asdict())
@example(**PINNED_FUNDING_CASES["one_cent_stock"]._asdict())
@example(**PINNED_FUNDING_CASES["one_cent_stock_approved"]._asdict())
def test_reserved_funding_never_exceeds_funding_available(balance: Decimal, cash_share: Decimal,
                                                           power_share: Decimal, profile: str,
                                                           crypto_minimum: Decimal, crypto_increment: Decimal,
                                                           entries: list[tuple[AssetIdentity, Decimal, Decimal,
                                                                               Decimal]]) -> None:
    results = asyncio.run(_check_funding(balance, cash_share, power_share, profile, crypto_minimum,
                                         crypto_increment, entries))
    event(f"approvals: {sum(result.status == 'pending' for result in results)}")


@pytest.mark.parametrize("name", PINNED_FUNDING_CASES)
def test_pinned_funding_cases_keep_their_outcome(name: str) -> None:
    """Final review M5: _check_funding only checks approved entries, so a
    pinned example that a later change refuses would pass vacuously. Each
    keeps its outcome; two of them reach approval."""
    results = asyncio.run(_check_funding(*PINNED_FUNDING_CASES[name]))
    assert [(result.status, result.reasons) for result in results] == PINNED_OUTCOMES[name]


# --- 4. protective exits never carry a DBAE reason (spec §9) -----------------------------------------------

_EXIT_EVIDENCE = st.sampled_from(["unknown", "stale", "blocked", "closing_only", "limited_margin", "verified"])


async def _check_exit(balance: Decimal, evidence: str, quantity: Decimal) -> None:
    _fresh_example()
    with tempfile.TemporaryDirectory() as directory:
        async with _held(Path(directory), balance) as held:
            rig = held.rig
            if evidence == "unknown":
                rig.caps.account = None
                rig.caps.assets = {}
                rig.caps.bars = []
            elif evidence == "stale":
                rig.caps.account = _account(balance, at=NOW - timedelta(days=1))
            elif evidence == "blocked":
                rig.caps.account = _account(balance, trading_blocked=True, account_blocked=None)
            elif evidence == "closing_only":
                rig.caps.account = _account(balance, closing_transactions_only=True)
            elif evidence == "limited_margin":
                rig.caps.account = _account(balance, account_multiplier=Decimal(1))

            result = await held.stop_exit(quantity)

            assert result.status == "filled", result.reasons
            assert not any("DBAE" in reason for reason in result.reasons)
            assert rig.caps.reads == []
            [intent] = await rig.intents()
            assert "dbae" not in intent.risk_snapshot and "DBAE" not in (intent.rejection_reason or "")
            assert await _events(rig, "dbae_entry_refused") == []
            assert await _events(rig, "dbae_band_changed") == []


@EXITS
@given(
    balance=HOLDING_BALANCE,
    evidence=_EXIT_EVIDENCE,
    quantity=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("0.05"), places=3, allow_nan=False,
                         allow_infinity=False),
)
def test_protective_exit_never_carries_a_dbae_reason(balance: Decimal, evidence: str, quantity: Decimal) -> None:
    asyncio.run(_check_exit(balance, evidence, quantity))
