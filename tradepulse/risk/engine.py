"""Deterministic, strategy-independent risk engine -- port of
base44/shared/riskEngine.ts. A DENIED result means ZERO units, never "at
least one unit". The risk engine has veto authority over every strategy and
AI signal.

Includes ONE addition beyond the port: check_cash_sufficiency(), called
unconditionally for BUY orders inside evaluate_risk(). The audited Base44
system had a fully-implemented cash-reservation function (reserveCash() in
cashLedger.ts) that was never called from anywhere in the execution path --
a confirmed pre-trade risk gap. Here the equivalent check cannot be skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradepulse.broker.types import AlpacaPosition
from tradepulse.models import (
    AssetClass,
    PortfolioSnapshot,
    RiskLimits,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    TradeIntentStatus,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.persistence import (
    PersistenceRepositories,
    hydrate,
    list_all_by_json_field,
    list_all_by_json_time_range,
    list_all_by_statuses,
    paginate_all_rows,
)

# Risk-day boundary for trades_today/daily_realized (resolves the previous
# UTC-calendar-day simplification): a single NY-midnight-to-NY-midnight
# window, DST-aware, applied uniformly across the whole account regardless
# of asset class. max_daily_trades/daily_pnl_pct are already single
# account-wide limits spanning equity+crypto+options together in one
# PortfolioSnapshot -- splitting the day boundary per asset class would mean
# two different "days" governing one shared counter, which doesn't resolve
# cleanly under that single-cap architecture. Matches this module's own
# prior nyDayStart()-referencing comment rather than inventing a new policy.
_NY_TZ = ZoneInfo("America/New_York")


def _risk_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    ny_midnight = now.astimezone(_NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return ny_midnight.astimezone(UTC), (ny_midnight + timedelta(days=1)).astimezone(UTC)


def _round_qty(qty: Decimal, asset_class: AssetClass) -> Decimal:
    """Floor at asset-appropriate precision so a cap never increases the
    requested notional. Equities use thousandths; crypto uses eight decimal
    places; options trade in whole contracts only."""
    precision = Decimal("100000000") if asset_class == AssetClass.CRYPTO else Decimal("1") if asset_class == AssetClass.OPTION else Decimal("1000")
    floored = (max(qty, Decimal("0")) * precision).to_integral_value(rounding=ROUND_FLOOR)
    return floored / precision


def round_quantity(qty: Decimal, asset_class: AssetClass) -> Decimal:
    """Public name for _round_qty: floor to the asset class's order precision."""
    return _round_qty(qty, asset_class)


def _minimum_quantity(asset_class: AssetClass) -> Decimal:
    if asset_class == AssetClass.CRYPTO:
        return Decimal("0.00000001")
    if asset_class == AssetClass.OPTION:
        return Decimal("1")
    return Decimal("0.001")


def _confidence_multiplier(confidence: Decimal | None, min_confidence: Decimal, floor_multiplier: Decimal) -> Decimal:
    """confidence is guaranteed >= min_confidence by the time this runs --
    anything lower already rejected via CONFIDENCE_BELOW_MIN earlier in
    evaluate_risk. None means the caller supplied no confidence signal at
    all -- no scaling, matches how the min_confidence gate itself already
    skips when confidence is None."""
    if confidence is None:
        return Decimal("1")
    span = Decimal("100") - min_confidence
    if span <= 0 or confidence >= 100:
        return Decimal("1")
    fraction = (confidence - min_confidence) / span
    return floor_multiplier + (Decimal("1") - floor_multiplier) * fraction


@dataclass(frozen=True, slots=True)
class RiskCheckInput:
    symbol: str
    asset_class: AssetClass
    side: Side
    requested_quantity: Decimal
    price: Decimal
    confidence: Decimal | None = None
    stop_loss: Decimal | None = None
    sector: str = "Other"
    # Dollar notional = quantity * price * contract_multiplier -- 1 for
    # equity/crypto, ~100 for a standard options contract. Callers derive
    # this once via models/market.py::contract_multiplier_of(asset), the
    # sole authority; evaluate_risk never reads asset metadata itself.
    contract_multiplier: Decimal = Decimal("1")
    # Market Regime Phase 2 -- a bare Decimal, deliberately: this module's
    # own docstring states it has "veto authority over every strategy...
    # deterministic, strategy-independent", so it must never import
    # strategy.regime types, only accept the already-computed multiplier
    # a caller derived from one. None means no regime signal was supplied
    # at all (every existing caller/test that doesn't pass this is
    # unaffected). evaluate_risk independently validates this is a finite
    # Decimal in [0, 1] before ever using it -- see the INVALID_REGIME_MULTIPLIER
    # gate below; it does not trust the caller just because the caller is
    # expected to only ever supply a value in range.
    regime_multiplier: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskEvalOptions:
    kill_switch: bool = False
    protective_exit: bool = False
    bid: Decimal | None = None
    ask: Decimal | None = None
    estimated_slippage_pct: Decimal | None = None
    max_drawdown_breached: bool = False
    skip_market_data_checks: bool = False
    held_quantity: Decimal = Decimal("0")
    held_notional: Decimal = Decimal("0")
    available_cash: Decimal | None = None
    estimated_fees: Decimal = Decimal("0")
    # Combined risk to stop of open positions and pending buys (open_portfolio_risk).
    open_risk: Decimal = Decimal("0")
    # Filled BUY intents for this symbol in the current risk day (symbol_entries_today).
    symbol_entries_today: int = 0


# Estimated taker fee per side, % of notional, used by the cost rule. Alpaca
# charges crypto 0.15-0.25% per side by volume tier (0.25% at the entry tier);
# US equity and option regulatory fees are a negligible fraction of R.
FEE_PCT_PER_SIDE = {AssetClass.CRYPTO: Decimal("0.25")}


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    approved_quantity: Decimal
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CashCheck:
    approved: bool
    reason: str | None
    max_affordable_notional: Decimal


@dataclass(frozen=True, slots=True)
class DrawdownCheck:
    breached: bool
    drawdown_pct: Decimal | None
    peak_equity: Decimal | None
    limit_pct: Decimal | None


def check_cash_sufficiency(
    cash_balance: Decimal,
    requested_notional: Decimal,
    estimated_fees: Decimal,
    buffer_pct: Decimal = Decimal("1"),
) -> CashCheck:
    """max_affordable_notional lets callers use this as a downward SIZING cap
    (see evaluate_risk's sizing block), not just a binary approve/reject --
    a partially-affordable trade should size down, not get a blanket no."""
    buffered_cash = cash_balance * (Decimal("1") - buffer_pct / Decimal("100"))
    max_affordable_notional = max(Decimal("0"), buffered_cash - estimated_fees)
    required = requested_notional + estimated_fees
    if required > buffered_cash:
        return CashCheck(False, f"INSUFFICIENT_CASH (required {required}, available {buffered_cash})", max_affordable_notional)
    return CashCheck(True, None, max_affordable_notional)


def evaluate_risk(
    intent: RiskCheckInput, snapshot: PortfolioSnapshot | None, limits: RiskLimits, opts: RiskEvalOptions | None = None
) -> RiskDecision:
    """snapshot may be None only for a sell: a protective exit or a plain sell
    returns before any portfolio figure is read (Rev.149 F04). New exposure
    without a snapshot is a caller defect and raises."""
    opts = opts or RiskEvalOptions()
    reasons: list[str] = []
    qty = intent.requested_quantity
    price = intent.price
    minimum_quantity = _minimum_quantity(intent.asset_class)

    if opts.kill_switch and not opts.protective_exit:
        return RiskDecision(False, Decimal("0"), ["KILL_SWITCH_ACTIVE"])

    # Spread limit -- fail closed if bid/ask unavailable or spread excessive.
    # internal_paper/shadow_live have no real market data (skip_market_data_checks).
    # Options carry their own, wider limits (see RiskLimits.spread_limit_for).
    spread_limit = limits.spread_limit_for(intent.asset_class)
    slippage_limit = limits.slippage_limit_for(intent.asset_class)
    if intent.side == Side.BUY and spread_limit and not opts.skip_market_data_checks:
        if opts.bid is None or opts.ask is None:
            reasons.append("NO_QUOTE_DATA_FOR_SPREAD_CHECK")
        else:
            mid = (opts.bid + opts.ask) / 2
            if mid > 0:
                spread_pct = ((opts.ask - opts.bid) / mid) * 100
                if spread_pct > spread_limit:
                    reasons.append(f"SPREAD_EXCEEDS_LIMIT ({spread_pct:.2f}% > {spread_limit}%)")

    # Slippage limit -- fail closed if no estimate supplied.
    if intent.side == Side.BUY and slippage_limit and not opts.skip_market_data_checks:
        if opts.estimated_slippage_pct is None:
            reasons.append("NO_SLIPPAGE_ESTIMATE")
        elif opts.estimated_slippage_pct > slippage_limit:
            reasons.append(
                f"SLIPPAGE_EXCEEDS_LIMIT ({opts.estimated_slippage_pct:.2f}% > {slippage_limit}%)"
            )

    if intent.side == Side.BUY and not opts.protective_exit and opts.max_drawdown_breached:
        reasons.append("MAX_DRAWDOWN_BREACHED")

    # Cost rule: a buy's estimated round-trip cost (full spread plus a fee
    # each side) must be a small fraction of the trade's own risk R, or the
    # edge is paid away in friction -- the same test at any account size.
    if (intent.side == Side.BUY and not opts.protective_exit
            and intent.stop_loss is not None and price > 0 and price > intent.stop_loss
            and opts.bid is not None and opts.ask is not None and not opts.skip_market_data_checks):
        mid = (opts.bid + opts.ask) / 2
        if mid > 0:
            cost_pct = (opts.ask - opts.bid) / mid * 100 + 2 * FEE_PCT_PER_SIDE.get(intent.asset_class, Decimal("0"))
            risk_pct = (price - intent.stop_loss) / price * 100
            if cost_pct > limits.max_cost_to_risk_pct / 100 * risk_pct:
                reasons.append(f"COST_EXCEEDS_RISK_FRACTION ({cost_pct:.2f}% > {limits.max_cost_to_risk_pct}% of R {risk_pct:.2f}%)")

    if opts.protective_exit:
        if reasons:
            return RiskDecision(False, Decimal("0"), reasons)
        return RiskDecision(True, qty, ["PROTECTIVE_EXIT"])

    if intent.side == Side.SELL:
        if opts.held_quantity < qty:
            return RiskDecision(
                False, Decimal("0"),
                [f"INSUFFICIENT_POSITION_TO_SELL (held {opts.held_quantity}, requested {qty})"],
            )
        return RiskDecision(True, qty, ["OK"])

    if snapshot is None:
        raise ValueError("RISK_SNAPSHOT_REQUIRED: new exposure is evaluated against a portfolio snapshot")

    if intent.confidence is not None and intent.confidence < limits.min_confidence:
        reasons.append(f"CONFIDENCE_BELOW_MIN ({intent.confidence} < {limits.min_confidence})")
    if intent.regime_multiplier is not None and (
        not intent.regime_multiplier.is_finite() or not (Decimal("0") <= intent.regime_multiplier <= Decimal("1"))
    ):
        # The deterministic risk engine validates this itself rather than
        # trusting the caller -- a regime_multiplier outside [0, 1], or
        # non-finite (NaN/Infinity), indicates a defect somewhere upstream
        # in strategy-layer code this module must not blindly trust (see
        # RiskCheckInput.regime_multiplier's own docstring). An outright
        # rejection here, not a silent skip/clamp -- this should never
        # actually trigger in normal operation.
        reasons.append(f"INVALID_REGIME_MULTIPLIER ({intent.regime_multiplier})")
    if snapshot.trades_today >= limits.max_daily_trades:
        reasons.append(f"MAX_DAILY_TRADES_REACHED ({snapshot.trades_today}/{limits.max_daily_trades})")
    if snapshot.open_positions >= limits.max_open_positions:
        reasons.append(f"MAX_OPEN_POSITIONS_REACHED ({snapshot.open_positions}/{limits.max_open_positions})")
    if snapshot.daily_pnl_pct <= -limits.max_daily_loss_pct:
        reasons.append(f"MAX_DAILY_LOSS_EXCEEDED ({snapshot.daily_pnl_pct:.2f}% <= -{limits.max_daily_loss_pct}%)")

    total_equity = snapshot.total_equity
    # Total exposure is NOT an early hard reject -- it's a downward SIZING
    # cap, applied below alongside max_position_pct/max_sector_pct/cash. A
    # request that would blow the exposure ceiling should size down to
    # whatever headroom remains, not get rejected outright while capacity
    # still exists (same principle as every other capital-allocation cap in
    # this function). See the max_total_exposure_pct cap inside the sizing
    # block below -- it already computes remaining_exposure and shrinks
    # approved_qty into it; zero remaining headroom naturally floors to a
    # zero-ish quantity, caught by the final minimum-lot/notional check.

    if opts.symbol_entries_today >= limits.max_entries_per_symbol_per_day:
        reasons.append(f"MAX_ENTRIES_PER_SYMBOL_REACHED ({opts.symbol_entries_today}/{limits.max_entries_per_symbol_per_day})")

    if limits.max_simultaneous_orders and snapshot.outstanding_orders >= limits.max_simultaneous_orders:
        reasons.append(
            f"MAX_SIMULTANEOUS_ORDERS_REACHED ({snapshot.outstanding_orders}/{limits.max_simultaneous_orders})"
        )

    if reasons:
        return RiskDecision(False, Decimal("0"), reasons)

    approved_qty = qty
    # Dollar notional for one unit of this instrument -- 1 unit = 1 share
    # for equity/crypto (multiplier 1), but 1 options contract represents
    # `contract_multiplier` (typically 100) units of underlying exposure.
    # Every notional/exposure computation below multiplies by this, INCLUDING
    # risk-based sizing's unit_risk (before the division that derives
    # quantity, not applied only to the notional caps afterward) -- applying
    # it only downstream could transiently size a position ~100x too large
    # before any cap catches it.
    notional_per_unit = price * intent.contract_multiplier
    if total_equity > 0 and price > 0:
        # Risk-based sizing: shares = risk_budget / unit_risk, then cap by
        # max_position_pct, max_sector_pct, total exposure, and cash.
        if intent.stop_loss is not None and intent.stop_loss > 0 and limits.max_risk_per_trade_pct:
            risk_budget = (limits.max_risk_per_trade_pct / 100) * total_equity
            # Confidence scales the ALLOWED RISK BUDGET, not the resulting
            # quantity directly -- ATR (via stop_loss) defines risk-per-unit,
            # confidence determines how much of that budget gets deployed.
            # intent.confidence is guaranteed >= min_confidence here (else
            # already rejected above via CONFIDENCE_BELOW_MIN); None means no
            # signal was supplied, so no scaling.
            confidence_multiplier = _confidence_multiplier(intent.confidence, limits.min_confidence, limits.min_position_size_multiplier)
            if confidence_multiplier < 1:
                risk_budget *= confidence_multiplier
                reasons.append(f"RISK_BUDGET_SCALED_BY_CONFIDENCE_TO_{(confidence_multiplier * 100).quantize(Decimal('0.1'))}PCT")
            # Regime scales the SAME risk budget, same shape as confidence,
            # applied immediately after it -- by this point regime_multiplier
            # is already proven finite and in [0, 1] (the INVALID_REGIME_MULTIPLIER
            # gate above returned before this line could ever run on a bad
            # value). Never scales up (only `< 1` multiplies); composes
            # multiplicatively with confidence, not independently capped --
            # both shrink the SAME budget before the same unconditional hard
            # caps below, so regime can never enlarge a position past any
            # existing limit regardless of how confidence_multiplier moved it.
            if intent.regime_multiplier is not None and intent.regime_multiplier < 1:
                risk_budget *= intent.regime_multiplier
                reasons.append(f"RISK_BUDGET_SCALED_BY_REGIME_TO_{(intent.regime_multiplier * 100).quantize(Decimal('0.1'))}PCT")
            unit_risk = abs(price - intent.stop_loss) * intent.contract_multiplier
            if unit_risk > 0:
                risk_based_qty = _round_qty(risk_budget / unit_risk, intent.asset_class)
                if risk_based_qty < approved_qty:
                    approved_qty = risk_based_qty
                    reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_RISK_BASED_SIZING")
                # Combined open risk: what every open position and pending buy
                # stands to lose at its stop, plus this buy's, stays within
                # max_open_risk_pct of equity. Positions protected at or above
                # break-even contribute nothing, which frees room for new trades.
                remaining_risk = (limits.max_open_risk_pct / 100) * total_equity - opts.open_risk
                capped_by_risk = _round_qty(max(remaining_risk, Decimal(0)) / unit_risk, intent.asset_class)
                if capped_by_risk < approved_qty:
                    approved_qty = capped_by_risk
                    reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_OPEN_RISK")

        # The cap bounds the whole position: notional already held in this
        # asset (broker market value, multiplier included) consumes it.
        max_position_notional = (limits.max_position_pct / 100) * total_equity - opts.held_notional
        if approved_qty * notional_per_unit > max_position_notional:
            approved_qty = _round_qty(max(max_position_notional, Decimal(0)) / notional_per_unit, intent.asset_class)
            reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_POSITION_PCT")

        # Rev.149 F06: unmanaged broker inventory has no known sector, so it
        # counts against every sector cap, never as a free pass.
        current_sector = snapshot.sector_exposure.get(intent.sector, Decimal("0"))
        if intent.sector != UNMANAGED_SECTOR:
            current_sector += snapshot.sector_exposure.get(UNMANAGED_SECTOR, Decimal(0))
        max_sector_notional = (limits.max_sector_pct / 100) * total_equity
        remaining_sector = max_sector_notional - current_sector
        if approved_qty * notional_per_unit > remaining_sector:
            capped_by_sector = _round_qty(remaining_sector / notional_per_unit, intent.asset_class) if notional_per_unit > 0 else Decimal("0")
            if capped_by_sector < approved_qty:
                approved_qty = capped_by_sector
                reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_SECTOR_PCT")

        if limits.max_total_exposure_pct:
            remaining_exposure = ((limits.max_total_exposure_pct / 100) * total_equity) - snapshot.holdings_value
            if approved_qty * notional_per_unit > remaining_exposure:
                capped_by_exposure = (
                    _round_qty(remaining_exposure / notional_per_unit, intent.asset_class) if notional_per_unit > 0 else Decimal("0")
                )
                if capped_by_exposure < approved_qty:
                    approved_qty = capped_by_exposure
                    reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_TOTAL_EXPOSURE")

        # Cash is a downward SIZING cap here, not an earlier hard reject --
        # a partially-affordable trade should size down (small-account
        # support), not get a blanket no. Never reached for SELL/
        # protective_exit (both return earlier in this function), so this
        # can only ever shrink genuine new/increasing exposure.
        if opts.available_cash is not None:
            cash_check = check_cash_sufficiency(opts.available_cash, approved_qty * notional_per_unit, opts.estimated_fees)
            if not cash_check.approved:
                capped_by_cash = _round_qty(cash_check.max_affordable_notional / notional_per_unit, intent.asset_class) if notional_per_unit > 0 else Decimal("0")
                if capped_by_cash < approved_qty:
                    approved_qty = capped_by_cash
                    reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_AVAILABLE_CASH")

    if approved_qty < minimum_quantity or (notional_per_unit > 0 and approved_qty * notional_per_unit < limits.min_lot_notional):
        reasons.append("INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT")
        return RiskDecision(False, Decimal("0"), reasons)

    if approved_qty >= qty:
        reasons.append("OK")
    return RiskDecision(True, max(Decimal("0"), approved_qty), reasons)


_PENDING_BUY_STATUSES = (
    TradeIntentStatus.RISK_APPROVED.value, TradeIntentStatus.SUBMITTED.value, TradeIntentStatus.ACCEPTED.value,
    TradeIntentStatus.PARTIALLY_FILLED.value, TradeIntentStatus.SUBMISSION_UNKNOWN.value,
)


# Rev.149 F06: the sector bucket for broker inventory no local holding
# accounts for. evaluate_risk counts it against every sector's cap.
UNMANAGED_SECTOR = "Unmanaged"


def unmanaged_excess(position: AlpacaPosition, local_quantity: Decimal) -> Decimal:
    """The broker market value of the part of ``position`` that local
    records do not account for (Rev.149 F06), valued per unit at the broker's
    own market value (contract multiplier included). Zero when local records
    cover the whole position."""
    broker_quantity = abs(position.qty)
    excess_quantity = broker_quantity - local_quantity
    if broker_quantity == 0 or excess_quantity <= 0:
        return Decimal(0)
    return abs(position.market_value) / broker_quantity * excess_quantity


async def open_portfolio_risk(repositories: PersistenceRepositories, *,
                              broker_positions: Mapping[str, AlpacaPosition] | None = None) -> Decimal:
    """What the account stands to lose if every open long and every pending
    buy hit its stop: sum of max(0, average_price - effective stop) x
    quantity x contract multiplier over holdings, plus the unfilled part of
    each pending BUY at (entry_price - stop_loss). A holding whose effective
    stop (current_stop if set, else stop_loss) is at or above its average
    price risks nothing; a holding with no stop counts its whole notional.

    Rev.149 F06: with ``broker_positions``, broker inventory beyond the local
    holding has no stop TradePulse manages, so it also counts its whole
    market value."""
    total = Decimal(0)
    held: dict[str, Decimal] = {}
    for row in await paginate_all_rows(repositories.holdings):
        holding = hydrate("holdings", row["payload"])
        held[asset_identity_key(holding.asset)] = abs(holding.quantity)
        if holding.quantity <= 0:
            continue
        stop = holding.current_stop if holding.current_stop is not None else holding.stop_loss
        per_unit = holding.average_price - (stop if stop is not None else Decimal(0))
        total += max(per_unit, Decimal(0)) * holding.quantity * contract_multiplier_of(holding.asset)
    for row in await list_all_by_statuses(repositories.trade_intents, list(_PENDING_BUY_STATUSES)):
        intent = hydrate("trade_intents", row["payload"])
        entry = intent.risk_snapshot.get("entry_price") if intent.risk_snapshot else None
        if intent.side != Side.BUY or intent.stop_loss is None or entry is None or intent.requested_quantity is None:
            continue
        unfilled = intent.requested_quantity - (intent.filled_quantity or Decimal(0))
        per_unit = Decimal(str(entry)) - intent.stop_loss
        total += max(per_unit, Decimal(0)) * max(unfilled, Decimal(0)) * contract_multiplier_of(intent.asset)
    for key, position in (broker_positions or {}).items():
        total += unmanaged_excess(position, held.get(key, Decimal(0)))
    return total


async def symbol_entries_today(repositories: PersistenceRepositories, asset, now: datetime, *,
                               exclude_intent_id: str | None = None) -> int:
    """Distinct BUY intents for this asset that either have a fill in the
    current NY risk day or are still pending (Rev.141: an entry in flight
    counts, so a second can't pass while the first is unfilled).
    ``exclude_intent_id`` is the intent being evaluated."""
    start, end = _risk_day_bounds(now)
    key = asset_identity_key(asset)
    rows = await list_all_by_json_time_range(repositories.fills, "filled_at", start, end)
    fills = [hydrate("fills", row["payload"]) for row in rows]
    entries = {f.trade_intent_id for f in fills if f.side == Side.BUY and asset_identity_key(f.asset) == key}
    for row in await list_all_by_statuses(repositories.trade_intents, list(_PENDING_BUY_STATUSES)):
        intent = hydrate("trade_intents", row["payload"])
        if intent.side == Side.BUY and asset_identity_key(intent.asset) == key:
            entries.add(intent.trade_intent_id)
    entries.discard(exclude_intent_id)
    return len(entries)


# Status-filtered, unbounded pagination -- NOT list_all(limit=1000), which is
# oldest-first and can silently undercount pending exposure once 1000 older
# (any-status) trade_intents exist. See
# persistence/repositories.py::list_all_by_statuses.
_PENDING_NOTIONAL_STATUSES = (
    TradeIntentStatus.RISK_APPROVED.value, TradeIntentStatus.SUBMITTED.value,
    TradeIntentStatus.ACCEPTED.value, TradeIntentStatus.PARTIALLY_FILLED.value,
    TradeIntentStatus.SUBMISSION_UNKNOWN.value,
)


@dataclass(frozen=True, slots=True)
class PendingIntentRemainder:
    """One pending intent's RISK-091-01 buckets.

    remaining_qty is requested_quantity minus every attributed
    SettlementEvent quantity, unclamped (an over-attributed intent is <= 0).
    unsettled_events are the attributed events not yet holding_projected,
    in repository order; unsettled_event_ids are their ids. row_status is
    the trade_intents status column the intent was selected by.
    requested_qty is the intent's requested_quantity, proven present."""

    intent: TradeIntent
    requested_qty: Decimal
    remaining_qty: Decimal
    unsettled_event_ids: frozenset[str]
    unsettled_events: tuple[SettlementEvent, ...]
    row_status: str


async def pending_intent_remainders(repositories: PersistenceRepositories) -> list[PendingIntentRemainder]:
    """Every intent in RISK_APPROVED, SUBMITTED, ACCEPTED, PARTIALLY_FILLED
    or SUBMISSION_UNKNOWN, either side, with its never-attributed remainder.

    RISK-091-01: corrected to never derive "how much of this order has the
    broker actually filled" from intent.filled_quantity --
    execution/gateway.py::_poll_and_settle only persists filled_quantity to
    the trade_intents row on a status transition, not on every poll
    iteration, so a later partial fill in the same poll loop can be durably
    attributed (a real Fill + SettlementEvent already created) while
    filled_quantity is still stale-low in the DB. Using filled_quantity here
    would make remaining_qty overcount the still-open portion of the order
    AND, combined with the unsettled-notional term, double-count that same
    fill. SettlementEvent records are the only ground truth for "attributed
    so far" -- they are created once per validated fill activity and never
    rewritten to lose history."""
    intent_rows = await list_all_by_statuses(repositories.trade_intents, list(_PENDING_NOTIONAL_STATUSES))
    remainders: list[PendingIntentRemainder] = []
    for row in intent_rows:
        intent: TradeIntent = hydrate("trade_intents", row["payload"])
        if intent.requested_quantity is None:
            # Every pending intent was sized at RISK_APPROVED; one without a
            # quantity has no remainder to reserve or count.
            raise ValueError(f"PENDING_INTENT_QUANTITY_MISSING: {intent.trade_intent_id}")
        event_rows = await list_all_by_json_field(repositories.settlements, "trade_intent_id", intent.trade_intent_id)
        events: list[SettlementEvent] = [hydrate("settlements", r["payload"]) for r in event_rows]

        total_attributed_qty = sum((e.quantity for e in events), Decimal("0"))
        unsettled = tuple(e for e in events if not e.holding_projected)
        remainders.append(PendingIntentRemainder(
            intent=intent,
            requested_qty=intent.requested_quantity,
            remaining_qty=intent.requested_quantity - total_attributed_qty,
            unsettled_event_ids=frozenset(e.settlement_event_id for e in unsettled),
            unsettled_events=unsettled,
            row_status=row["status"],
        ))
    return remainders


async def build_portfolio_snapshot(
    repositories: PersistenceRepositories,
    *,
    cash_balance: Decimal,
    account_equity: Decimal | None = None,
    broker_prev_close_equity: Decimal | None = None,
    mark_prices: Mapping[str, Decimal] | None = None,
    broker_positions: Mapping | None = None,
    now: datetime | None = None,
) -> PortfolioSnapshot:
    """Caller supplies cash_balance (from the broker account or the local
    cash ledger -- this function has no opinion on which) and, for
    broker-backed accounts, account_equity/broker_prev_close_equity so daily
    P&L uses the SAME definition the execution gateway uses, matching the
    audited Base44 fix for scan/execution using contradictory equity sources.

    mark_prices is keyed by canonical asset identity
    (models/market.py::asset_identity_key), NOT display symbol -- a
    ticker-shaped symbol can be shared by economically distinct instruments,
    so the caller (see execution/gateway.py) must build it the same way.

    "today" is a single NY-midnight-to-NY-midnight risk day (see
    _risk_day_bounds), applied uniformly across the whole account
    regardless of asset class -- resolves the prior UTC-calendar-day
    simplification.
    """
    now = now or datetime.now(UTC)
    mark_prices = mark_prices or {}

    # FIN-090-01: unbounded, whole-table pagination -- NOT list_all(),
    # which defaults to limit=1000 (oldest-first) and would silently
    # understate holdings_value/sector_exposure -- and therefore make
    # max_total_exposure_pct/max_sector_pct limit checks more PERMISSIVE,
    # not less -- once enough holdings existed first. See the sibling
    # trade_intents pull below for the same fix already applied earlier.
    holding_rows = await paginate_all_rows(repositories.holdings)
    holdings = [hydrate("holdings", row["payload"]) for row in holding_rows]

    from tradepulse.reconciliation.epochs import pending_assets
    pending_keys = await pending_assets(repositories)
    pending_positions: Mapping = {}
    if pending_keys:
        if broker_positions is None or account_equity is None:
            raise ValueError("PENDING_CRYPTO_REQUIRES_BROKER_EXPOSURE")
        pending_positions = broker_positions

    holdings_value = Decimal("0")
    sector_exposure: dict[str, Decimal] = {}
    # Rev.149 F06: the quantity of each asset that local records already
    # value -- holdings plus filled-but-unsettled buys. Broker inventory beyond
    # it is added below as unmanaged exposure, so nothing is counted twice.
    local_quantity: dict[str, Decimal] = {}
    for holding in holdings:
        key = asset_identity_key(holding.asset)
        local_quantity[key] = local_quantity.get(key, Decimal(0)) + abs(holding.quantity)
        if key in pending_keys:
            continue
        mark = mark_prices.get(asset_identity_key(holding.asset), holding.average_price)
        notional = abs(holding.quantity) * mark * contract_multiplier_of(holding.asset)
        holdings_value += notional
        sector = holding.sector or "Other"
        sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + notional

    # Rev.140 §3.3: quarantined inventory is real exposure, always at the
    # adverse (higher) of its last confirmed and current mark.
    from tradepulse.quarantine import quarantined_positions
    quarantined_keys: set[str] = set()
    for record in await quarantined_positions(repositories):
        quarantined_keys.add(record.asset_key)
        exposure = record.exposure(mark_prices.get(record.asset_key, record.last_confirmed_mark or record.reference_price))
        holdings_value += exposure
        sector_exposure["Quarantined"] = sector_exposure.get("Quarantined", Decimal("0")) + exposure

    sectors_by_key = {asset_identity_key(h.asset): h.sector or "Other" for h in holdings}
    for key in pending_keys:
        position = pending_positions.get(key)
        if position is None:
            continue  # complete broker response proves zero current inventory
        notional = abs(position.market_value)
        holdings_value += notional
        sector = sectors_by_key.get(key, "Other")
        sector_exposure[sector] = sector_exposure.get(sector, Decimal(0)) + notional

    # RISK-091-01 buckets: see pending_intent_remainders. Every unit of a
    # pending intent's requested_quantity falls into exactly one of three
    # buckets: never-attributed (remaining_qty), attributed-but-not-yet-
    # settled (added once here, keyed by settlement_event_id), or
    # attributed-and-settled (already reflected in Holdings above,
    # untouched by either term). A terminal-status intent (e.g. already
    # FILLED, so absent from the pending set entirely) only has the latter
    # two buckets -- covered by the global catch-all term further below plus
    # Holdings.
    remainders = await pending_intent_remainders(repositories)
    already_counted_settlement_ids: set[str] = set()
    for remainder in remainders:
        intent = remainder.intent
        if remainder.remaining_qty > 0 and intent.reference_price is not None:
            pending_notional = remainder.remaining_qty * intent.reference_price * contract_multiplier_of(intent.asset)
            holdings_value += pending_notional
            sector = intent.sector or "Other"
            sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + pending_notional

        for event in remainder.unsettled_events:
            if event.side == Side.BUY:
                event_key = asset_identity_key(event.asset)
                local_quantity[event_key] = local_quantity.get(event_key, Decimal(0)) + event.quantity
            if asset_identity_key(event.asset) in pending_keys:
                continue
            unsettled_notional = event.quantity * event.price * contract_multiplier_of(event.asset)
            holdings_value += unsettled_notional
            sector = event.sector or "Other"
            sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + unsettled_notional
            already_counted_settlement_ids.add(event.settlement_event_id)

    # Global catch-all: every non-COMPLETED settlement (COMPLETED is the
    # only status where holding_projected is guaranteed true, since it's
    # only reached after all settlement stages -- including
    # holding_projected -- succeed). This is what catches a still-unsettled
    # fill belonging to a TERMINAL-status intent (e.g. already FILLED, so
    # invisible to the per-intent loop above), and correctly also a
    # permanently-stuck TERMINAL_FAILED/INTEGRITY_BLOCKED settlement --
    # that is still real, unresolved broker exposure our own pipeline gave
    # up projecting, and must still count.
    #
    # Deduplicated by settlement_event_id, not trade_intent_id: an event
    # newly attributed for an intent already processed by the per-intent
    # loop above would otherwise be silently invisible to both terms --
    # exactly the class of gap this fix closes. Keying the dedup on the
    # event's own id instead means it doesn't matter which of the two
    # queries happens to observe a given event first; it is still counted
    # exactly once.
    unsettled_rows = await list_all_by_statuses(
        repositories.settlements,
        [
            SettlementStatus.PENDING.value, SettlementStatus.PROCESSING.value, SettlementStatus.RETRYABLE_FAILED.value,
            SettlementStatus.TERMINAL_FAILED.value, SettlementStatus.INTEGRITY_BLOCKED.value,
        ],
    )
    for row in unsettled_rows:
        event = hydrate("settlements", row["payload"])
        if event.holding_projected or event.settlement_event_id in already_counted_settlement_ids:
            continue
        if event.side == Side.BUY:
            event_key = asset_identity_key(event.asset)
            local_quantity[event_key] = local_quantity.get(event_key, Decimal(0)) + event.quantity
        if asset_identity_key(event.asset) in pending_keys:
            continue
        unsettled_notional = event.quantity * event.price * contract_multiplier_of(event.asset)
        holdings_value += unsettled_notional
        sector = event.sector or "Other"
        sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + unsettled_notional

    # Rev.149 F06: broker inventory with no local holding (inherited with
    # adoption off, or unmanaged before reconciliation catches it) is real
    # account exposure. Pending-epoch and quarantined assets are already
    # valued from broker evidence above.
    broker_keys: set[str] = set()
    for key, position in (broker_positions or {}).items():
        if position.qty == 0:
            continue
        broker_keys.add(key)
        if key in pending_keys or key in quarantined_keys:
            continue
        excess = unmanaged_excess(position, local_quantity.get(key, Decimal(0)))
        if excess > 0:
            holdings_value += excess
            sector_exposure[UNMANAGED_SECTOR] = sector_exposure.get(UNMANAGED_SECTOR, Decimal(0)) + excess

    total_equity = account_equity if account_equity and account_equity > 0 else holdings_value

    # Rev.149 F03: count limits reserve commitments exactly as dollars do.
    # Every pending intent is an order: RISK_APPROVED included, because the
    # portfolio lock is released before it reaches the broker, and
    # SUBMISSION_UNKNOWN because it may still be live there. A definitive
    # rejection, cancel or expiry leaves the pending set and releases it.
    outstanding_orders = len(remainders)
    pending_entries = [r.intent for r in remainders if r.intent.side == Side.BUY]
    # A position slot is a distinct asset held or committed to: a holding,
    # quarantined or broker inventory, or a pending entry. An add-on to a
    # held asset takes no second slot.
    position_keys = ({asset_identity_key(h.asset) for h in holdings} | quarantined_keys | broker_keys
                     | {asset_identity_key(i.asset) for i in pending_entries})

    risk_day_start, risk_day_end = _risk_day_bounds(now)
    # Time-window query, unbounded pagination -- NOT list_all(limit=1000)
    # filtered by now.date() in Python. A single economic trade can produce
    # many partial-fill rows (confirmed live: one order filled in 5
    # pieces), so "a day's rows are under 1000" is not a safe assumption;
    # this fetches every fill in the risk day regardless of row count.
    fill_rows = await list_all_by_json_time_range(repositories.fills, "filled_at", risk_day_start, risk_day_end)
    fills = [hydrate("fills", row["payload"]) for row in fill_rows]
    # Distinct trades, not fill rows -- a single order can fill in several
    # partial broker fills, which must count once against max_daily_trades,
    # not once per fill. Entries only (Rev.127): TradePulse opens long
    # positions only, so its BUYs are entries and its SELLs are exits; an
    # exit must not use up the day's allowance for new positions.
    # Rev.149 F03: a pending entry uses the day's allowance as soon as it is
    # approved, not when it first fills; one filled today and still pending
    # counts once.
    trades_today = len({f.trade_intent_id for f in fills if f.side == Side.BUY}
                       | {i.trade_intent_id for i in pending_entries})

    source: Literal["broker", "holdings"]
    if broker_prev_close_equity and broker_prev_close_equity > 0 and account_equity and account_equity > 0:
        daily_pnl_pct = ((account_equity - broker_prev_close_equity) / broker_prev_close_equity) * 100
        source = "broker"
    else:
        pnl_rows = await list_all_by_json_time_range(repositories.pnl_records, "as_of", risk_day_start, risk_day_end)
        pnl_records = [hydrate("pnl_records", row["payload"]) for row in pnl_rows]
        # Exact Decimal sum over every persisted record in the window --
        # never a SQL SUM/AVG aggregate, which would force a lossy float
        # round-trip on a financial total.
        daily_realized = sum((p.realized for p in pnl_records), Decimal("0"))
        daily_pnl_pct = (daily_realized / total_equity) * 100 if total_equity > 0 else Decimal("0")
        source = "holdings"

    return PortfolioSnapshot(
        snapshot_id=str(uuid4()),
        as_of=now,
        total_equity=total_equity,
        cash_balance=cash_balance,
        holdings_value=holdings_value,
        sector_exposure=sector_exposure,
        open_positions=len(position_keys),
        outstanding_orders=outstanding_orders,
        trades_today=trades_today,
        daily_pnl_pct=daily_pnl_pct,
        source=source,
    )


async def check_max_drawdown(
    repositories: PersistenceRepositories, current_equity: Decimal, limits: RiskLimits
) -> DrawdownCheck:
    if not limits.max_drawdown_pct or limits.max_drawdown_pct <= 0:
        return DrawdownCheck(False, None, None, limits.max_drawdown_pct)
    # A genuine all-time-peak search, not a row-capped scan -- SQL finds the
    # single largest-total_equity row directly (see
    # persistence/repositories.py::max_by_json_field), so a real historical
    # peak can never be missed just because the equity_snapshots table has
    # grown past some fixed limit.
    peak_row = await repositories.equity_snapshots.max_by_json_field("total_equity")
    peak = current_equity
    if peak_row is not None:
        historical_peak = Decimal(str(peak_row["payload"]["total_equity"]))
        if historical_peak > peak:
            peak = historical_peak
    if peak <= 0:
        return DrawdownCheck(False, None, peak, limits.max_drawdown_pct)
    drawdown_pct = ((peak - current_equity) / peak) * 100
    return DrawdownCheck(drawdown_pct >= limits.max_drawdown_pct, drawdown_pct, peak, limits.max_drawdown_pct)
