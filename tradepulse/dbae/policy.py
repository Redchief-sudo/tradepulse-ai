"""The pure DBAE entry policy (spec §4.1, §5, §6, §8.0–§8.4).

No I/O and no clock reads: every input, including ``now``, is passed in.
Evidence values of None are unknown and never permissive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from tradepulse.broker.capabilities import AccountEvidence, AssetTradingCapabilities, evidence_fresh
from tradepulse.dbae import evidence
from tradepulse.dbae.bands import Band, band_for, floor_to_increment, unlocks
from tradepulse.models.enums import AssetClass
from tradepulse.models.market import AssetIdentity, contract_multiplier_of

_ONE_DOLLAR = Decimal(1)
_WHOLE = Decimal(1)
_MICRO_BANDS = frozenset({Band.MICRO, Band.MICRO_PLUS})


@dataclass(frozen=True)
class EntryProposal:
    asset: AssetIdentity
    price: Decimal
    stop_price: Decimal
    broker_environment: Literal["alpaca-paper", "alpaca-live"]
    now: datetime


@dataclass(frozen=True)
class ExecutionPolicy:
    allowed: bool
    reasons: tuple[str, ...]
    band: Band
    band_equity: Decimal | None
    fractional_allowed: bool

    def as_snapshot(self) -> dict[str, str | bool | list[str] | None]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "band": self.band.value,
            "band_equity": None if self.band_equity is None else str(self.band_equity),
            "fractional_allowed": self.fractional_allowed,
        }


@dataclass(frozen=True)
class ShapedQuantity:
    quantity: Decimal
    rule: Literal["unchanged", "whole_shares", "crypto_increment"]
    refusal: str | None


def _refused(reasons: list[str], band: Band, band_equity: Decimal | None) -> ExecutionPolicy:
    return ExecutionPolicy(False, tuple(reasons), band, band_equity, False)


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _core_account_reasons(account: AccountEvidence, now: datetime) -> list[str]:
    """§5.2 and §5.3, in §10's order. Any reason here ends the evaluation."""
    if not evidence_fresh(account.received_at, now):
        return ["DBAE_ACCOUNT_STATE_UNVERIFIED"]
    reasons: list[str] = []
    if account.status is None:
        _add(reasons, "DBAE_ACCOUNT_STATE_UNVERIFIED")
    elif account.status != "ACTIVE":
        _add(reasons, "DBAE_ACCOUNT_NOT_ACTIVE")
    blocking = (
        account.trading_blocked,
        account.account_blocked,
        account.trade_suspended_by_user,
        account.suspend_trade,
    )
    if any(flag is True for flag in blocking):
        _add(reasons, "DBAE_ACCOUNT_TRADING_BLOCKED")
    if account.closing_transactions_only is True:
        _add(reasons, "DBAE_ACCOUNT_CLOSING_ONLY")
    if any(flag is None for flag in (*blocking, account.closing_transactions_only)):
        _add(reasons, "DBAE_ACCOUNT_STATE_UNVERIFIED")
    if account.account_multiplier == 1:
        _add(reasons, "DBAE_ACCOUNT_TYPE_UNVERIFIED")
    elif account.account_multiplier not in evidence.ACCEPTED_ACCOUNT_MULTIPLIERS:
        _add(reasons, "DBAE_ACCOUNT_STATE_UNVERIFIED")
    if account.equity is None or account.cash is None or account.buying_power is None:
        _add(reasons, "DBAE_ACCOUNT_STATE_UNVERIFIED")
    return reasons


def _class_permission_reason(account: AccountEvidence, asset_class: AssetClass) -> str | None:
    """§5.2's class-dependent rows. A missing crypto or options field never blocks equity."""
    if asset_class is AssetClass.CRYPTO:
        if account.crypto_status != "ACTIVE" or account.non_marginable_buying_power is None:
            return "DBAE_CRYPTO_PERMISSION_UNVERIFIED"
    elif asset_class is AssetClass.OPTION:
        level = account.options_trading_level
        if level is None or level < evidence.REQUIRED_OPTIONS_LEVEL:
            return "DBAE_OPTIONS_PERMISSION_UNVERIFIED"
    return None


def _class_unlocked(band: Band, asset_class: AssetClass) -> bool:
    band_unlocks = unlocks(band)
    if asset_class is AssetClass.EQUITY:
        return band_unlocks.equity != "none"
    if asset_class is AssetClass.CRYPTO:
        return band_unlocks.crypto
    if asset_class is AssetClass.OPTION:
        return band_unlocks.options
    return False


def _positive(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def evaluate_entry_policy(
    account: AccountEvidence,
    band_equity: Decimal | None,
    asset: AssetTradingCapabilities,
    proposal: EntryProposal,
) -> ExecutionPolicy:
    """Spec §8.1 steps 1–3: account evidence, baseline and band, asset eligibility."""
    band = Band.BELOW_MINIMUM if band_equity is None else band_for(band_equity)
    asset_class = proposal.asset.asset_class

    # Step 1: account evidence (§5).
    reasons = _core_account_reasons(account, proposal.now)
    if reasons:
        return _refused(reasons, band, band_equity)
    permission = _class_permission_reason(account, asset_class)
    if permission is not None:
        reasons.append(permission)

    # Step 2: session baseline and band (§4).
    if band_equity is None:
        reasons.append("DBAE_SESSION_BASELINE_UNVERIFIED")
        return _refused(reasons, band, band_equity)
    if band is Band.BELOW_MINIMUM:
        reasons.append("DBAE_BELOW_MINIMUM_BALANCE")
        return _refused(reasons, band, band_equity)

    # Step 3: asset eligibility (§6).
    # Stale asset evidence is unknown evidence: every field reads as None and refuses under its own reason.
    if not evidence_fresh(asset.received_at, proposal.now):
        asset = AssetTradingCapabilities(None, None, None, None, None, None, asset.received_at)
    if not _class_unlocked(band, asset_class):
        _add(reasons, "DBAE_ASSET_CLASS_LOCKED")
    if asset.active is not True or asset.tradable is not True:
        _add(reasons, "DBAE_ASSET_NOT_TRADABLE")
    fractional_allowed = False
    if asset_class is AssetClass.EQUITY:
        if band in _MICRO_BANDS:
            if asset.fractionable is not True:
                _add(reasons, "DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND")
            if account.fractional_trading is not True:
                _add(reasons, "DBAE_FRACTIONAL_TRADING_UNVERIFIED")
        # Outside MICRO/MICRO_PLUS a non-fractional equity is floored to whole shares (§8.2), not refused.
        fractional_allowed = account.fractional_trading is True and asset.fractionable is True
    elif asset_class is AssetClass.CRYPTO:
        if not (_positive(asset.min_order_size) and _positive(asset.min_trade_increment)):
            _add(reasons, "DBAE_CRYPTO_INCREMENT_UNVERIFIED")
        # Crypto quantities are governed by the asset's increment, not account fractional trading.
        fractional_allowed = not reasons

    if reasons:
        return _refused(reasons, band, band_equity)
    return ExecutionPolicy(True, (), band, band_equity, fractional_allowed)


def _require_shapable(policy: ExecutionPolicy, qty: Decimal) -> None:
    if not policy.allowed:
        raise ValueError("a refused policy cannot shape or exit a quantity")
    if not qty.is_finite() or qty <= 0:
        raise ValueError("approved quantity must be positive")


def shape_quantity(
    policy: ExecutionPolicy,
    asset: AssetTradingCapabilities,
    asset_class: AssetClass,
    approved_qty: Decimal,
) -> ShapedQuantity:
    """Spec §8.2: round down only, never up."""
    _require_shapable(policy, approved_qty)
    if asset_class is AssetClass.CRYPTO:
        increment = asset.min_trade_increment
        minimum = asset.min_order_size
        if increment is None or minimum is None or not (_positive(increment) and _positive(minimum)):
            return ShapedQuantity(Decimal(0), "crypto_increment", "DBAE_CRYPTO_INCREMENT_UNVERIFIED")
        floored = floor_to_increment(approved_qty, increment)
        if floored < minimum:
            return ShapedQuantity(floored, "crypto_increment", "DBAE_BELOW_ASSET_MINIMUM")
        return ShapedQuantity(floored, "crypto_increment", None)
    if asset_class is AssetClass.EQUITY and policy.fractional_allowed:
        # The risk engine already sized it at 0.001 precision; the re-approval run checks it (§7.1).
        return ShapedQuantity(approved_qty, "unchanged", None)
    # Non-fractional equities and options trade whole units only.
    whole = floor_to_increment(approved_qty, _WHOLE)
    if whole == 0:
        return ShapedQuantity(whole, "whole_shares", "WHOLE_SHARE_UNAFFORDABLE")
    return ShapedQuantity(whole, "whole_shares", None)


def liquidation_threshold(asset: AssetTradingCapabilities, price: Decimal) -> Decimal:
    """Spec §8.0: max($1, min_order_size × price); the second term only when the asset has a minimum."""
    if asset.min_order_size is None:
        return _ONE_DOLLAR
    return max(_ONE_DOLLAR, asset.min_order_size * price)


def _is_whole(qty: Decimal) -> bool:
    return qty == qty.to_integral_value()


def _on_increment(qty: Decimal, increment: Decimal) -> bool:
    return qty % increment == 0


def exit_feasible(
    policy: ExecutionPolicy,
    asset: AssetTradingCapabilities,
    proposal: EntryProposal,
    qty: Decimal,
) -> str | None:
    """Spec §8.3: the refusal reason if the protective exit would not be a supported order, else None."""
    _require_shapable(policy, qty)
    asset_class = proposal.asset.asset_class
    fractional = not _is_whole(qty)
    if asset_class is AssetClass.CRYPTO:
        increment = asset.min_trade_increment
        minimum = asset.min_order_size
        if increment is None or minimum is None or not (_positive(increment) and _positive(minimum)):
            return "DBAE_CRYPTO_INCREMENT_UNVERIFIED"
        if not _on_increment(qty, increment):
            return "DBAE_CRYPTO_INCREMENT_UNVERIFIED"
        if qty < minimum:
            return "DBAE_BELOW_ASSET_MINIMUM"
    elif fractional and not policy.fractional_allowed:
        return "DBAE_FRACTIONAL_TRADING_UNVERIFIED"

    stop_value = qty * proposal.stop_price * contract_multiplier_of(proposal.asset)
    if stop_value >= liquidation_threshold(asset, proposal.stop_price):
        return None
    crypto = asset_class is AssetClass.CRYPTO
    scope = evidence.LiquidationScope(
        broker_environment=proposal.broker_environment,
        account_type="margin",
        asset_class=asset_class,
        quantity_kind="fractional" if fractional else "whole",
        order_type="market",
        time_in_force="gtc" if crypto else "day",
        session="regular",
        minimum_kind="notional_below_1_usd_qty_at_or_above_min_order_size" if crypto else "notional_below_1_usd",
    )
    # Exact match only, read at call time: no wildcard, no inheritance between scopes.
    verdict = evidence.SUB_MINIMUM_LIQUIDATION_EVIDENCE.get(scope)
    if verdict is not None and verdict.verdict == "verified":
        return None
    return "DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED"


def scale_out_feasible(
    asset: AssetTradingCapabilities | None,
    asset_class: AssetClass,
    contract_multiplier: Decimal,
    part: Decimal,
    rest: Decimal,
    price: Decimal,
) -> bool:
    """Spec §8.4: both parts at least the asset minimum, and on the increment for crypto."""
    if part <= 0 or rest <= 0:
        return False
    if asset_class is AssetClass.CRYPTO:
        if asset is None:
            return False
        increment = asset.min_trade_increment
        if increment is None or not _positive(increment) or not _positive(asset.min_order_size):
            return False
        if not (_on_increment(part, increment) and _on_increment(rest, increment)):
            return False
    threshold = _ONE_DOLLAR if asset is None else liquidation_threshold(asset, price)
    return all(leg * price * contract_multiplier >= threshold for leg in (part, rest))

