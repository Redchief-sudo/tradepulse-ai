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
    dbae.max_funding that does not reconcile exactly with the block's
    frozen inputs (DBAE-146-01). They add
    nothing to total, so the caller must refuse entries while any exist."""

    total: Decimal
    mismatches: tuple[str, ...]
    unpriced: tuple[str, ...]


def buy_fee_pct(asset_class: AssetClass) -> Decimal:
    """The buy-side fee rate (%): crypto's taker fee, zero for equities and
    options (no commission; regulatory fees are sell-side)."""
    return FEE_PCT_PER_SIDE.get(asset_class, Decimal(0))


def projected_buy_fee(asset_class: AssetClass, notional: Decimal) -> Decimal:
    """The estimated buy-side fee on ``notional`` at buy_fee_pct."""
    return notional * buy_fee_pct(asset_class) / 100


def funding_for(quantity: Decimal, reference_price: Decimal, contract_multiplier: Decimal, slippage_pct: Decimal,
                fee_pct: Decimal) -> Decimal:
    """§7.2, the one formula: order notional × (1 + slippage_pct / 100) +
    order notional × fee_pct / 100, where the order notional is quantity ×
    reference_price × contract_multiplier. The fee is projected on the
    order notional, not on the slippage-buffered figure.

    max_funding prices a new buy with it; pending_buy_funding recomputes a
    frozen record with it from the record's own inputs (DBAE-146-01). The
    same Decimal expression on the same inputs gives the same Decimal."""
    notional = quantity * reference_price * contract_multiplier
    return notional * (1 + slippage_pct / 100) + notional * fee_pct / 100


def max_funding(submitted_qty: Decimal, reference_price: Decimal, asset: AssetIdentity, slippage_pct: Decimal) -> Decimal:
    """§7.2: funding_for at contract_multiplier_of(asset) and the asset
    class's buy-side fee rate. An option without a contract multiplier
    raises ValueError."""
    return funding_for(submitted_qty, reference_price, contract_multiplier_of(asset), slippage_pct,
                       buy_fee_pct(asset.asset_class))


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
    rate = 1 + slippage_pct / 100 + buy_fee_pct(asset_class) / 100
    return available / rate


def _frozen_decimal(block: Mapping[str, object], key: str, *, allow_zero: bool = False) -> Decimal | None:
    """block[key] as a finite Decimal above zero (or at zero, with
    ``allow_zero``), or None when it holds no such value."""
    raw = block.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def _dbae_block(intent: TradeIntent) -> Mapping[str, object] | None:
    block = intent.risk_snapshot.get("dbae")
    return block if isinstance(block, Mapping) else None


def _frozen_max_funding(block: Mapping[str, object], submitted_qty: Decimal, intent: TradeIntent) -> Decimal | None:
    """DBAE-146-01: dbae.max_funding, the funding committed at RISK_APPROVED
    for dbae.submitted_quantity, when the record proves it.

    The block freezes the inputs max_funding was computed from:
    reference_price, contract_multiplier, slippage_pct and fee_pct. The
    record is accepted only when every one of them and max_funding is a
    finite positive Decimal (slippage_pct and fee_pct may be zero), the frozen
    reference_price is the intent's, the frozen contract_multiplier is
    contract_multiplier_of(intent.asset), and funding_for recomputes
    max_funding from them exactly (zero tolerance). Otherwise None: the buy
    is unpriced (DBAE_FUNDING_UNVERIFIED). No fixed ratio bounds the
    record. An option without a contract multiplier raises ValueError, as
    max_funding does."""
    frozen = _frozen_decimal(block, "max_funding")
    price = _frozen_decimal(block, "reference_price")
    multiplier = _frozen_decimal(block, "contract_multiplier")
    slippage = _frozen_decimal(block, "slippage_pct", allow_zero=True)
    fee = _frozen_decimal(block, "fee_pct", allow_zero=True)
    if frozen is None or price is None or multiplier is None or slippage is None or fee is None:
        return None
    if price != intent.reference_price or multiplier != contract_multiplier_of(intent.asset):
        return None
    if funding_for(submitted_qty, price, multiplier, slippage, fee) != frozen:
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
    block (pre-DBAE: no frozen parameters), a dbae block that does not
    reconcile (_frozen_max_funding), or no reference_price."""
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
        recorded = None if block is None else _frozen_decimal(block, "submitted_quantity")
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
