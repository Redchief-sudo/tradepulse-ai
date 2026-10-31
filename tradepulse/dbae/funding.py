"""DBAE funding reservations (spec §7).

A buy's funding stays committed, across every symbol, from its lease-fenced
RISK_APPROVED write until a fill is attributed to it or the broker confirms
a terminal state. The outstanding commitment P is derived from durable
intents and SettlementEvents on every call; there is no running total.

Dollar notional is always quantity × price × contract_multiplier_of(asset).
The account's buying-power multiplier never enters any figure here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tradepulse.broker.capabilities import AccountEvidence
from tradepulse.models import AssetClass, RiskLimits, Side, TradeIntent, contract_multiplier_of
from tradepulse.models.market import AssetIdentity
from tradepulse.persistence import PersistenceRepositories
from tradepulse.risk.engine import FEE_PCT_PER_SIDE, pending_intent_remainders


@dataclass(frozen=True, slots=True)
class PendingFunding:
    """P (§7.4) and the pending buys it could not be computed exactly for.

    mismatches: ids whose requested_quantity differs from
    risk_snapshot.dbae.submitted_quantity (reserved on the larger).
    unpriced: ids with an unattributed remainder that cannot be priced (no
    reference_price, or an unparsable dbae.submitted_quantity); they add
    nothing to total, so the caller must refuse entries while any exist."""

    total: Decimal
    mismatches: tuple[str, ...]
    unpriced: tuple[str, ...]


def projected_buy_fee(asset_class: AssetClass, notional: Decimal) -> Decimal:
    """The estimated buy-side fee on ``notional``: crypto's taker fee, zero
    for equities and options (no commission; regulatory fees are sell-side)."""
    return notional * FEE_PCT_PER_SIDE.get(asset_class, Decimal(0)) / 100


def max_funding(submitted_qty: Decimal, reference_price: Decimal, asset: AssetIdentity, slippage_pct: Decimal) -> Decimal:
    """§7.2: order notional × (1 + slippage_pct / 100) + projected fee.

    The order notional is submitted_qty × reference_price ×
    contract_multiplier_of(asset). The fee is projected on that order
    notional, not on the slippage-buffered figure. An option without a
    contract multiplier raises ValueError."""
    notional = submitted_qty * reference_price * contract_multiplier_of(asset)
    return notional * (1 + slippage_pct / 100) + projected_buy_fee(asset.asset_class, notional)


def _dbae_submitted_quantity(intent: TradeIntent) -> Decimal | None:
    """risk_snapshot.dbae.submitted_quantity as a finite positive Decimal,
    or None when there is no dbae block or it holds no such quantity."""
    block = intent.risk_snapshot.get("dbae")
    if not isinstance(block, Mapping):
        return None
    raw = block.get("submitted_quantity")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        quantity = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not quantity.is_finite() or quantity <= 0:
        return None
    return quantity


async def pending_buy_funding(repositories: PersistenceRepositories, limits: RiskLimits) -> PendingFunding:
    """§7.4: the funding still committed to every pending buy, all symbols.

    Per buy in RISK_APPROVED, SUBMITTED, ACCEPTED, PARTIALLY_FILLED or
    SUBMISSION_UNKNOWN: max_funding on (submitted quantity − attributed
    quantity), attributed quantity from SettlementEvents. The fee term is
    projected on the unattributed remainder only, so a fee recorded on an
    attributed portion (or a correction of it) never adds to P. Sells never
    reserve funding."""
    total = Decimal(0)
    mismatches: list[str] = []
    unpriced: list[str] = []
    for remainder in await pending_intent_remainders(repositories):
        intent = remainder.intent
        # requested_quantity is never None here: pending_intent_remainders
        # raises PENDING_INTENT_QUANTITY_MISSING first.
        if intent.side != Side.BUY or intent.requested_quantity is None:
            continue
        field_qty = intent.requested_quantity
        attributed_qty = field_qty - remainder.remaining_qty
        recorded = _dbae_submitted_quantity(intent)
        if recorded is None and "dbae" in intent.risk_snapshot:
            # A dbae block without a usable submitted quantity: the amount
            # committed is unknown, so it cannot be priced.
            unpriced.append(intent.trade_intent_id)
            continue
        submitted_qty = field_qty
        if recorded is not None and recorded != field_qty:
            mismatches.append(intent.trade_intent_id)
            submitted_qty = max(field_qty, recorded)
        unattributed_qty = submitted_qty - attributed_qty
        if unattributed_qty <= 0:
            continue
        if intent.reference_price is None:
            # Flagged, never skipped. A buy with nothing left to fund
            # (above) needs no price and is not flagged.
            unpriced.append(intent.trade_intent_id)
            continue
        total += max_funding(
            unattributed_qty, intent.reference_price, intent.asset, limits.slippage_limit_for(intent.asset.asset_class),
        )
    return PendingFunding(total=total, mismatches=tuple(mismatches), unpriced=tuple(unpriced))


def funding_available(account: AccountEvidence, asset_class: AssetClass, pending: Decimal) -> Decimal | None:
    """§7.5: min(cash − P, buying_power − P), with non_marginable_buying_power
    in place of buying_power for crypto. None when any input is missing or
    negative (DBAE_FUNDING_UNVERIFIED); a result at or below zero is
    returned as is (INSUFFICIENT_CASH)."""
    power = account.non_marginable_buying_power if asset_class == AssetClass.CRYPTO else account.buying_power
    cash = account.cash
    if cash is None or power is None or cash < 0 or power < 0 or pending < 0:
        return None
    return min(cash - pending, power - pending)
