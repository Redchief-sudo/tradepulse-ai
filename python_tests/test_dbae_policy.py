import ast
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import pytest

from tradepulse.broker.capabilities import AccountEvidence, AssetTradingCapabilities
from tradepulse.dbae import Band, LiquidationScope, LiquidationVerdict, evidence
from tradepulse.dbae.policy import (
    EntryProposal,
    ExecutionPolicy,
    ShapedQuantity,
    evaluate_entry_policy,
    exit_feasible,
    liquidation_threshold,
    scale_out_feasible,
    shape_quantity,
)
from tradepulse.models.enums import AssetClass
from tradepulse.models.market import AssetIdentity

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
FULL_EQUITY = Decimal(5000)

EQUITY = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
CRYPTO = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
OPTION = AssetIdentity(
    "AAPL261120C00200000", AssetClass.OPTION, "alpaca:AAPL261120C00200000", contract_multiplier=100
)


def _account(**changes: object) -> AccountEvidence:
    base = AccountEvidence(
        status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        suspend_trade=False,
        closing_transactions_only=False,
        fractional_trading=True,
        equity=FULL_EQUITY,
        cash=FULL_EQUITY,
        buying_power=FULL_EQUITY * 4,
        non_marginable_buying_power=FULL_EQUITY,
        account_multiplier=Decimal(4),
        crypto_status="ACTIVE",
        options_trading_level=2,
        received_at=NOW,
    )
    return replace(base, **changes)


def _equity_caps(**changes: object) -> AssetTradingCapabilities:
    base = AssetTradingCapabilities(
        active=True,
        tradable=True,
        fractionable=True,
        min_order_size=None,
        min_trade_increment=None,
        price_increment=Decimal("0.01"),
        received_at=NOW,
    )
    return replace(base, **changes)


def _crypto_caps(**changes: object) -> AssetTradingCapabilities:
    base = AssetTradingCapabilities(
        active=True,
        tradable=True,
        fractionable=True,
        min_order_size=Decimal("0.0001"),
        min_trade_increment=Decimal("0.000000001"),
        price_increment=Decimal(1),
        received_at=NOW,
    )
    return replace(base, **changes)


def _option_caps() -> AssetTradingCapabilities:
    return _equity_caps(fractionable=False)


def _proposal(
    asset: AssetIdentity = EQUITY,
    price: str = "100",
    stop: str = "95",
    environment: Literal["alpaca-paper", "alpaca-live"] = "alpaca-paper",
) -> EntryProposal:
    return EntryProposal(
        asset=asset, price=Decimal(price), stop_price=Decimal(stop), broker_environment=environment, now=NOW
    )


def _allowed(fractional: bool = True) -> ExecutionPolicy:
    return ExecutionPolicy(
        allowed=True, reasons=(), band=Band.FULL, band_equity=FULL_EQUITY, fractional_allowed=fractional
    )


# --- §5.2, §5.3, §10: account rows ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "ACCOUNT_CLOSED"}, "DBAE_ACCOUNT_NOT_ACTIVE"),
        ({"status": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"trading_blocked": True}, "DBAE_ACCOUNT_TRADING_BLOCKED"),
        ({"account_blocked": True}, "DBAE_ACCOUNT_TRADING_BLOCKED"),
        ({"trade_suspended_by_user": True}, "DBAE_ACCOUNT_TRADING_BLOCKED"),
        ({"suspend_trade": True}, "DBAE_ACCOUNT_TRADING_BLOCKED"),
        ({"closing_transactions_only": True}, "DBAE_ACCOUNT_CLOSING_ONLY"),
        ({"trading_blocked": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"account_blocked": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"trade_suspended_by_user": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"suspend_trade": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"closing_transactions_only": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"equity": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"cash": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"buying_power": None}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"account_multiplier": Decimal(1)}, "DBAE_ACCOUNT_TYPE_UNVERIFIED"),
        ({"received_at": NOW - timedelta(seconds=31)}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        ({"received_at": NOW + timedelta(seconds=6)}, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
    ],
)
def test_each_blocking_flag_refuses_with_its_reason(changes: dict[str, object], reason: str) -> None:
    policy = evaluate_entry_policy(_account(**changes), FULL_EQUITY, _equity_caps(), _proposal())
    assert not policy.allowed
    assert policy.reasons == (reason,)


def test_core_failure_returns_before_baseline_and_asset_checks() -> None:
    policy = evaluate_entry_policy(
        _account(trading_blocked=True), None, _equity_caps(active=False), _proposal()
    )
    assert policy.reasons == ("DBAE_ACCOUNT_TRADING_BLOCKED",)


def test_account_reasons_are_collected_in_check_order() -> None:
    policy = evaluate_entry_policy(
        _account(status="INACTIVE", suspend_trade=True, closing_transactions_only=True, cash=None),
        FULL_EQUITY,
        _equity_caps(),
        _proposal(),
    )
    assert policy.reasons == (
        "DBAE_ACCOUNT_NOT_ACTIVE",
        "DBAE_ACCOUNT_TRADING_BLOCKED",
        "DBAE_ACCOUNT_CLOSING_ONLY",
        "DBAE_ACCOUNT_STATE_UNVERIFIED",
    )


@pytest.mark.parametrize(
    ("multiplier", "reason"),
    [
        (Decimal(1), "DBAE_ACCOUNT_TYPE_UNVERIFIED"),
        (Decimal(3), "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        (Decimal(0), "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        (None, "DBAE_ACCOUNT_STATE_UNVERIFIED"),
        (Decimal(2), None),
        (Decimal(4), None),
    ],
)
def test_account_multiplier_values(multiplier: Decimal | None, reason: str | None) -> None:
    policy = evaluate_entry_policy(
        _account(account_multiplier=multiplier), FULL_EQUITY, _equity_caps(), _proposal()
    )
    if reason is None:
        assert policy.allowed
        assert policy.reasons == ()
    else:
        assert not policy.allowed
        assert policy.reasons == (reason,)


def test_missing_options_level_blocks_only_options() -> None:
    account = _account(options_trading_level=None)
    equity = evaluate_entry_policy(account, FULL_EQUITY, _equity_caps(), _proposal())
    assert equity.allowed
    option = evaluate_entry_policy(account, FULL_EQUITY, _option_caps(), _proposal(OPTION, "2", "1.5"))
    assert option.reasons == ("DBAE_OPTIONS_PERMISSION_UNVERIFIED",)
    level_one = evaluate_entry_policy(
        _account(options_trading_level=1), FULL_EQUITY, _option_caps(), _proposal(OPTION, "2", "1.5")
    )
    assert level_one.reasons == ("DBAE_OPTIONS_PERMISSION_UNVERIFIED",)


def test_missing_crypto_fields_block_only_crypto() -> None:
    account = _account(crypto_status=None, non_marginable_buying_power=None)
    assert evaluate_entry_policy(account, FULL_EQUITY, _equity_caps(), _proposal()).allowed
    crypto = evaluate_entry_policy(account, FULL_EQUITY, _crypto_caps(), _proposal(CRYPTO, "60000", "57000"))
    assert crypto.reasons == ("DBAE_CRYPTO_PERMISSION_UNVERIFIED",)
    inactive = evaluate_entry_policy(
        _account(crypto_status="INACTIVE"), FULL_EQUITY, _crypto_caps(), _proposal(CRYPTO, "60000", "57000")
    )
    assert inactive.reasons == ("DBAE_CRYPTO_PERMISSION_UNVERIFIED",)


@pytest.mark.parametrize(
    "changes",
    [{"min_order_size": None}, {"min_trade_increment": None}, {"min_order_size": Decimal(0)},
     {"min_trade_increment": Decimal("-0.1")}],
)
def test_crypto_increment_unverified(changes: dict[str, object]) -> None:
    policy = evaluate_entry_policy(
        _account(), FULL_EQUITY, _crypto_caps(**changes), _proposal(CRYPTO, "60000", "57000")
    )
    assert policy.reasons == ("DBAE_CRYPTO_INCREMENT_UNVERIFIED",)


# --- §4: baseline and bands ------------------------------------------------------------------------


def test_missing_baseline_refuses() -> None:
    policy = evaluate_entry_policy(_account(), None, _equity_caps(), _proposal())
    assert policy.reasons == ("DBAE_SESSION_BASELINE_UNVERIFIED",)
    assert policy.band_equity is None


def test_below_minimum_balance_refuses() -> None:
    policy = evaluate_entry_policy(_account(), Decimal("19.99"), _equity_caps(), _proposal())
    assert policy.reasons == ("DBAE_BELOW_MINIMUM_BALANCE",)
    assert policy.band is Band.BELOW_MINIMUM


def test_band_locks() -> None:
    crypto = evaluate_entry_policy(
        _account(), Decimal("99.99"), _crypto_caps(), _proposal(CRYPTO, "60000", "57000")
    )
    assert crypto.reasons == ("DBAE_ASSET_CLASS_LOCKED",)
    assert evaluate_entry_policy(
        _account(), Decimal(100), _crypto_caps(), _proposal(CRYPTO, "60000", "57000")
    ).allowed
    option_locked = evaluate_entry_policy(
        _account(), Decimal("1999.99"), _option_caps(), _proposal(OPTION, "2", "1.5")
    )
    assert option_locked.reasons == ("DBAE_ASSET_CLASS_LOCKED",)
    option = evaluate_entry_policy(_account(), Decimal(2000), _option_caps(), _proposal(OPTION, "2", "1.5"))
    assert option.allowed
    assert option.band is Band.FULL
    assert option.fractional_allowed is False


def test_unsupported_asset_class_is_locked() -> None:
    forex = AssetIdentity("EUR/USD", AssetClass.FOREX, "alpaca:EUR/USD")
    policy = evaluate_entry_policy(_account(), FULL_EQUITY, _equity_caps(), _proposal(forex, "1.1", "1.0"))
    assert policy.reasons == ("DBAE_ASSET_CLASS_LOCKED",)


# --- §6: asset eligibility -------------------------------------------------------------------------


@pytest.mark.parametrize("changes", [{"active": False}, {"tradable": False}, {"active": None}, {"tradable": None}])
def test_asset_not_tradable(changes: dict[str, object]) -> None:
    policy = evaluate_entry_policy(_account(), FULL_EQUITY, _equity_caps(**changes), _proposal())
    assert policy.reasons == ("DBAE_ASSET_NOT_TRADABLE",)


def test_stale_asset_evidence_is_unknown() -> None:
    stale = _equity_caps(received_at=NOW - timedelta(seconds=31))
    policy = evaluate_entry_policy(_account(), FULL_EQUITY, stale, _proposal())
    assert policy.reasons == ("DBAE_ASSET_NOT_TRADABLE",)


def test_micro_refuses_whole_share_only_stock() -> None:
    for band_equity in (Decimal(20), Decimal("99.99")):
        policy = evaluate_entry_policy(_account(), band_equity, _equity_caps(fractionable=False), _proposal())
        assert policy.reasons == ("DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND",)
    whole_ok = evaluate_entry_policy(_account(), Decimal(100), _equity_caps(fractionable=False), _proposal())
    assert whole_ok.allowed
    assert whole_ok.fractional_allowed is False


def test_micro_needs_account_fractional_trading() -> None:
    for flag in (False, None):
        policy = evaluate_entry_policy(_account(fractional_trading=flag), Decimal(20), _equity_caps(), _proposal())
        assert policy.reasons == ("DBAE_FRACTIONAL_TRADING_UNVERIFIED",)
    # Outside MICRO/MICRO_PLUS the quantity falls back to whole shares instead.
    fallback = evaluate_entry_policy(_account(fractional_trading=None), Decimal(100), _equity_caps(), _proposal())
    assert fallback.allowed
    assert fallback.fractional_allowed is False


def test_micro_fractionable_equity_allowed() -> None:
    policy = evaluate_entry_policy(_account(), Decimal(20), _equity_caps(), _proposal())
    assert policy.allowed
    assert policy.band is Band.MICRO
    assert policy.fractional_allowed is True


def test_snapshot_is_json_safe() -> None:
    policy = evaluate_entry_policy(_account(), Decimal(20), _equity_caps(fractionable=False), _proposal())
    assert policy.as_snapshot() == {
        "allowed": False,
        "reasons": ["DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND"],
        "band": "micro",
        "band_equity": "20",
        "fractional_allowed": False,
    }
    missing = evaluate_entry_policy(_account(), None, _equity_caps(), _proposal()).as_snapshot()
    assert missing["band_equity"] is None


# --- §8.2: shaping ----------------------------------------------------------------------------------


def test_shape_fractional_unchanged() -> None:
    shaped = shape_quantity(_allowed(), _equity_caps(), AssetClass.EQUITY, Decimal("3.333"))
    assert shaped == ShapedQuantity(Decimal("3.333"), "unchanged", None)


def test_shape_whole_shares() -> None:
    policy = _allowed(fractional=False)
    caps = _equity_caps(fractionable=False)
    shaped = shape_quantity(policy, caps, AssetClass.EQUITY, Decimal("2.7"))
    assert shaped.quantity == Decimal(2)
    assert shaped.rule == "whole_shares"
    assert shaped.refusal is None
    tiny = shape_quantity(policy, caps, AssetClass.EQUITY, Decimal("0.4"))
    assert tiny.refusal == "WHOLE_SHARE_UNAFFORDABLE"
    assert tiny.quantity == Decimal(0)


def test_shape_crypto_increment() -> None:
    shaped = shape_quantity(
        _allowed(), _crypto_caps(), AssetClass.CRYPTO, Decimal("0.00123456789")
    )
    assert shaped == ShapedQuantity(Decimal("0.001234567"), "crypto_increment", None)


def test_shape_crypto_below_minimum() -> None:
    shaped = shape_quantity(_allowed(), _crypto_caps(), AssetClass.CRYPTO, Decimal("0.00009"))
    assert shaped.refusal == "DBAE_BELOW_ASSET_MINIMUM"
    assert shaped.rule == "crypto_increment"


def test_shape_crypto_without_increment_refuses() -> None:
    shaped = shape_quantity(
        _allowed(), _crypto_caps(min_trade_increment=None), AssetClass.CRYPTO, Decimal(1)
    )
    assert shaped.refusal == "DBAE_CRYPTO_INCREMENT_UNVERIFIED"


@pytest.mark.parametrize("qty", [Decimal(0), Decimal(-1)])
def test_shape_rejects_non_positive(qty: Decimal) -> None:
    with pytest.raises(ValueError, match="approved quantity must be positive"):
        shape_quantity(_allowed(), _equity_caps(), AssetClass.EQUITY, qty)


def test_shape_rejects_refused_policy() -> None:
    refused = ExecutionPolicy(False, ("DBAE_ASSET_NOT_TRADABLE",), Band.FULL, FULL_EQUITY, False)
    with pytest.raises(ValueError, match="refused policy"):
        shape_quantity(refused, _equity_caps(), AssetClass.EQUITY, Decimal(1))


def test_shape_never_rounds_up() -> None:
    # Seeded-random property over every shaping path (hypothesis is not a project dependency).
    rng = random.Random(20261030)
    cases = [
        (_allowed(), _equity_caps(), AssetClass.EQUITY),
        (_allowed(fractional=False), _equity_caps(fractionable=False), AssetClass.EQUITY),
        (_allowed(fractional=False), _option_caps(), AssetClass.OPTION),
        (_allowed(), _crypto_caps(), AssetClass.CRYPTO),
        (_allowed(), _crypto_caps(min_trade_increment=Decimal("0.25"),
                                                   min_order_size=Decimal("0.5")), AssetClass.CRYPTO),
    ]
    for _ in range(4000):
        policy, caps, asset_class = rng.choice(cases)
        approved = Decimal(rng.randint(1, 10**12)).scaleb(-rng.randint(0, 12))
        shaped = shape_quantity(policy, caps, asset_class, approved)
        assert shaped.quantity <= approved
        assert shaped.quantity >= 0
        if shaped.refusal is None:
            assert shaped.quantity > 0


# --- §8.0, §8.3: exit feasibility -------------------------------------------------------------------

PAPER_EQUITY_FRACTIONAL = LiquidationScope(
    broker_environment="alpaca-paper",
    account_type="margin",
    asset_class=AssetClass.EQUITY,
    quantity_kind="fractional",
    order_type="market",
    time_in_force="day",
    session="regular",
    minimum_kind="notional_below_1_usd",
)


def _table(verdict: Literal["verified", "refused"]) -> MappingProxyType[LiquidationScope, LiquidationVerdict]:
    return MappingProxyType(
        {PAPER_EQUITY_FRACTIONAL: LiquidationVerdict(verdict, "evidence/g2/manifest.json", "sha256:abc")}
    )


def test_liquidation_threshold() -> None:
    assert liquidation_threshold(_equity_caps(), Decimal(95)) == Decimal(1)
    assert liquidation_threshold(_crypto_caps(), Decimal(57000)) == Decimal("5.7000")
    assert liquidation_threshold(_crypto_caps(), Decimal(5000)) == Decimal(1)


def test_exit_above_threshold_needs_no_evidence() -> None:
    assert exit_feasible(_allowed(), _equity_caps(), _proposal(stop="95"), Decimal("0.011")) is None


def test_sub_minimum_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    equity_policy = _allowed()
    # $1.00 entry, $0.95 at the stop (spec §8.6 conservative case).
    sub_minimum = _proposal(price="100", stop="95")
    qty = Decimal("0.01")
    reason = "DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED"
    assert exit_feasible(equity_policy, _equity_caps(), sub_minimum, qty) == reason

    monkeypatch.setattr(evidence, "SUB_MINIMUM_LIQUIDATION_EVIDENCE", _table("verified"))
    assert exit_feasible(equity_policy, _equity_caps(), sub_minimum, qty) is None
    # Exact scope only: live, whole-share, crypto and option scopes are not covered.
    live = _proposal(price="100", stop="95", environment="alpaca-live")
    assert exit_feasible(equity_policy, _equity_caps(), live, qty) == reason
    whole = _proposal(price="0.9", stop="0.5")
    assert exit_feasible(_allowed(fractional=False), _equity_caps(fractionable=False),
                         whole, Decimal(1)) == reason
    crypto = _proposal(CRYPTO, price="1000", stop="900")
    assert exit_feasible(_allowed(), _crypto_caps(), crypto, Decimal("0.001")) == reason
    option = _proposal(OPTION, price="0.01", stop="0.005")
    assert exit_feasible(_allowed(fractional=False), _option_caps(), option, Decimal(1)) == reason

    monkeypatch.setattr(evidence, "SUB_MINIMUM_LIQUIDATION_EVIDENCE", _table("refused"))
    assert exit_feasible(equity_policy, _equity_caps(), sub_minimum, qty) == reason


def test_exit_option_value_includes_contract_multiplier() -> None:
    # 1 contract x $0.02 x 100 = $2.00 at the stop: above $1, no evidence needed.
    option = _proposal(OPTION, price="0.03", stop="0.02")
    assert exit_feasible(_allowed(fractional=False), _option_caps(), option, Decimal(1)) is None


def test_exit_fractional_without_fractional_allowed_refuses() -> None:
    policy = _allowed(fractional=False)
    assert exit_feasible(policy, _equity_caps(fractionable=False), _proposal(), Decimal("2.5")) == (
        "DBAE_FRACTIONAL_TRADING_UNVERIFIED"
    )


def test_exit_crypto_off_increment_or_below_minimum_refuses() -> None:
    crypto = _proposal(CRYPTO, price="60000", stop="57000")
    caps = _crypto_caps(min_trade_increment=Decimal("0.001"), min_order_size=Decimal("0.002"))
    assert exit_feasible(_allowed(), caps, crypto, Decimal("0.0025")) == (
        "DBAE_CRYPTO_INCREMENT_UNVERIFIED"
    )
    assert exit_feasible(_allowed(), caps, crypto, Decimal("0.001")) == "DBAE_BELOW_ASSET_MINIMUM"
    assert exit_feasible(_allowed(), caps, crypto, Decimal("0.002")) is None


# --- §8.4: scale-outs -------------------------------------------------------------------------------


def test_scale_out_feasible() -> None:
    one = Decimal(1)
    price = Decimal(2)
    assert scale_out_feasible(_equity_caps(), AssetClass.EQUITY, one, Decimal("0.5"), Decimal("0.5"), price)
    assert scale_out_feasible(None, AssetClass.EQUITY, one, Decimal("0.5"), Decimal("0.5"), price)
    assert not scale_out_feasible(_equity_caps(), AssetClass.EQUITY, one, Decimal("0.495"), Decimal(5), price)
    assert not scale_out_feasible(_equity_caps(), AssetClass.EQUITY, one, Decimal(5), Decimal("0.495"), price)

    caps = _crypto_caps(min_trade_increment=Decimal("0.001"), min_order_size=Decimal("0.001"))
    btc = Decimal(60000)
    assert scale_out_feasible(caps, AssetClass.CRYPTO, one, Decimal("0.002"), Decimal("0.003"), btc)
    assert not scale_out_feasible(caps, AssetClass.CRYPTO, one, Decimal("0.0025"), Decimal("0.003"), btc)
    assert not scale_out_feasible(caps, AssetClass.CRYPTO, one, Decimal("0.002"), Decimal("0.0035"), btc)
    assert not scale_out_feasible(None, AssetClass.CRYPTO, one, Decimal("0.002"), Decimal("0.003"), btc)
    # Below min_order_size x price.
    big_min = _crypto_caps(min_trade_increment=Decimal("0.001"), min_order_size=Decimal("0.003"))
    assert not scale_out_feasible(big_min, AssetClass.CRYPTO, one, Decimal("0.002"), Decimal("0.003"), btc)

    # Options: 1 contract x $0.02 x 100 = $2.00 per part.
    hundred = Decimal(100)
    assert scale_out_feasible(_option_caps(), AssetClass.OPTION, hundred, one, one, Decimal("0.02"))
    assert not scale_out_feasible(_option_caps(), AssetClass.OPTION, hundred, one, one, Decimal("0.0099"))


def test_scale_out_non_positive_part_is_not_feasible() -> None:
    one = Decimal(1)
    assert not scale_out_feasible(_equity_caps(), AssetClass.EQUITY, one, Decimal(0), Decimal(5), Decimal(10))


# --- purity ------------------------------------------------------------------------------------------


def test_policy_module_is_pure() -> None:
    source = Path(__file__).resolve().parents[1] / "tradepulse" / "dbae" / "policy.py"
    tree = ast.parse(source.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    forbidden = ("tradepulse.persistence", "tradepulse.execution", "tradepulse.broker.alpaca_client")
    assert not [name for name in imported if name.startswith(forbidden)]
    banned_calls = {"now", "utcnow", "today", "time", "monotonic", "open", "sleep"}
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not calls & banned_calls
