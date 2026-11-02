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
    unpriced: ids that cannot be priced: an unparsable
    dbae.submitted_quantity (flagged even when fully attributed), or an
    unattributed remainder with no reference_price, no dbae block, or a
    missing or inconsistent dbae.max_funding (DBAE-146-01). They add
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


def fundable_notional(available: Decimal, asset_class: AssetClass, slippage_pct: Decimal) -> Decimal:
    """The order notional whose max_funding (§7.2) equals ``available``:
    available / (1 + slippage_pct / 100 + the buy-side fee rate).

    The gateway passes this, not funding_available itself, to evaluate_risk
    as available_cash. The engine's cash cap bounds the order notional with
    a 1% buffer, but max_funding adds the slippage allowance (up to 1.75%
    for options) and crypto's fee on top of that notional, so sizing
    against funding_available alone could approve a buy whose reservation
    exceeds the funding available (found by the Task 10 property test).
    A result at or below zero stays at or below zero (INSUFFICIENT_CASH)."""
    rate = 1 + slippage_pct / 100 + FEE_PCT_PER_SIDE.get(asset_class, Decimal(0)) / 100
    return available / rate


# A frozen max_funding above this multiple of its order notional is not a
# slippage allowance plus a fee but a corrupt record (DBAE-146-01). Every
# profile allows at most 1.75% slippage and the largest buy-side fee is 0.25%.
MAX_FROZEN_FUNDING_RATE = Decimal(2)


def _positive_field(block: Mapping[str, object], key: str) -> Decimal | None:
    """block[key] as a finite positive Decimal, or None when it holds no such value."""
    raw = block.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _dbae_block(intent: TradeIntent) -> Mapping[str, object] | None:
    block = intent.risk_snapshot.get("dbae")
    return block if isinstance(block, Mapping) else None


def _frozen_max_funding(block: Mapping[str, object], submitted_qty: Decimal, intent: TradeIntent) -> Decimal | None:
    """DBAE-146-01: dbae.max_funding, the funding committed at RISK_APPROVED
    for dbae.submitted_quantity. None when max_funding is
    missing, not a finite positive number, or inconsistent with
    submitted_quantity × reference_price × contract multiplier: below the
    order notional plus its buy-side fee (a negative slippage), or above
    MAX_FROZEN_FUNDING_RATE times it. The bounds are compared without
    division, so they are exact. An option without a contract multiplier
    raises ValueError, as max_funding does."""
    frozen = _positive_field(block, "max_funding")
    price = intent.reference_price
    if frozen is None or price is None or not price.is_finite() or price <= 0:
        return None
    notional = submitted_qty * price * contract_multiplier_of(intent.asset)
    if frozen < notional + projected_buy_fee(intent.asset.asset_class, notional):
        return None
    if frozen > notional * MAX_FROZEN_FUNDING_RATE:
        return None
    return frozen


async def pending_buy_funding(repositories: PersistenceRepositories, limits: RiskLimits) -> PendingFunding:
    """§7.4: the funding still committed to every pending buy, all symbols.

    Per buy in RISK_APPROVED, SUBMITTED, ACCEPTED, PARTIALLY_FILLED or
    SUBMISSION_UNKNOWN, on the unattributed quantity (submitted quantity −
    quantity attributed by SettlementEvents):

    - the frozen commitment, dbae.max_funding / dbae.submitted_quantity per
      unit (DBAE-146-01), so a later profile change never reduces it; or
    - max_funding at the current profile's slippage, when that is larger:
      a profile change can only raise P.

    An attributed fill reduces P (each SettlementEvent counted once by
    pending_intent_remainders); a broker-confirmed terminal state drops the
    buy out of P; SUBMISSION_UNKNOWN keeps the full commitment. The fee term
    is on the unattributed remainder only, so a fee recorded on an
    attributed portion (or a correction of it) never adds to P. Sells never
    reserve funding.

    A buy with an unattributed remainder is unpriced when it has no dbae
    block (pre-DBAE: no frozen parameters), an unusable dbae block, or no
    reference_price."""
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
        block = _dbae_block(intent)
        recorded = None if block is None else _positive_field(block, "submitted_quantity")
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
            # Nothing left to fund needs no price and is not flagged.
            continue
        frozen = None if block is None or recorded is None else _frozen_max_funding(block, recorded, intent)
        if frozen is None or recorded is None or intent.reference_price is None:
            # No frozen authorization to reconstruct the commitment from
            # (a pre-DBAE buy, or an unusable max_funding), or no price.
            unpriced.append(intent.trade_intent_id)
            continue
        current = max_funding(
            unattributed_qty, intent.reference_price, intent.asset, limits.slippage_limit_for(intent.asset.asset_class),
        )
        # Multiplied before dividing: a wholly unattributed buy reserves its
        # frozen max_funding exactly.
        total += max(frozen * unattributed_qty / recorded, current)
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
