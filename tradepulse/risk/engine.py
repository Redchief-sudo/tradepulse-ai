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
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import uuid4

from tradepulse.models import (
    AssetClass,
    PortfolioSnapshot,
    RiskLimits,
    Side,
    TradeIntentStatus,
    asset_identity_key,
    contract_multiplier_of,
)
from tradepulse.persistence import PersistenceRepositories, hydrate


def _round_qty(qty: Decimal, asset_class: AssetClass) -> Decimal:
    """Floor at asset-appropriate precision so a cap never increases the
    requested notional. Equities use thousandths; crypto uses eight decimal
    places; options trade in whole contracts only."""
    precision = Decimal("100000000") if asset_class == AssetClass.CRYPTO else Decimal("1") if asset_class == AssetClass.OPTION else Decimal("1000")
    floored = (max(qty, Decimal("0")) * precision).to_integral_value(rounding=ROUND_FLOOR)
    return floored / precision


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
    available_cash: Decimal | None = None
    estimated_fees: Decimal = Decimal("0")


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
    intent: RiskCheckInput, snapshot: PortfolioSnapshot, limits: RiskLimits, opts: RiskEvalOptions | None = None
) -> RiskDecision:
    opts = opts or RiskEvalOptions()
    reasons: list[str] = []
    qty = intent.requested_quantity
    price = intent.price
    minimum_quantity = _minimum_quantity(intent.asset_class)

    if opts.kill_switch and not opts.protective_exit:
        return RiskDecision(False, Decimal("0"), ["KILL_SWITCH_ACTIVE"])

    # Spread limit -- fail closed if bid/ask unavailable or spread excessive.
    # internal_paper/shadow_live have no real market data (skip_market_data_checks).
    if intent.side == Side.BUY and limits.spread_limit_pct and not opts.skip_market_data_checks:
        if opts.bid is None or opts.ask is None:
            reasons.append("NO_QUOTE_DATA_FOR_SPREAD_CHECK")
        else:
            mid = (opts.bid + opts.ask) / 2
            if mid > 0:
                spread_pct = ((opts.ask - opts.bid) / mid) * 100
                if spread_pct > limits.spread_limit_pct:
                    reasons.append(f"SPREAD_EXCEEDS_LIMIT ({spread_pct:.2f}% > {limits.spread_limit_pct}%)")

    # Slippage limit -- fail closed if no estimate supplied.
    if intent.side == Side.BUY and limits.slippage_limit_pct and not opts.skip_market_data_checks:
        if opts.estimated_slippage_pct is None:
            reasons.append("NO_SLIPPAGE_ESTIMATE")
        elif opts.estimated_slippage_pct > limits.slippage_limit_pct:
            reasons.append(
                f"SLIPPAGE_EXCEEDS_LIMIT ({opts.estimated_slippage_pct:.2f}% > {limits.slippage_limit_pct}%)"
            )

    if intent.side == Side.BUY and not opts.protective_exit and opts.max_drawdown_breached:
        reasons.append("MAX_DRAWDOWN_BREACHED")

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

    if intent.confidence is not None and intent.confidence < limits.min_confidence:
        reasons.append(f"CONFIDENCE_BELOW_MIN ({intent.confidence} < {limits.min_confidence})")
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
            unit_risk = abs(price - intent.stop_loss) * intent.contract_multiplier
            if unit_risk > 0:
                risk_based_qty = _round_qty(risk_budget / unit_risk, intent.asset_class)
                if risk_based_qty < approved_qty:
                    approved_qty = risk_based_qty
                    reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_RISK_BASED_SIZING")

        max_position_notional = (limits.max_position_pct / 100) * total_equity
        if approved_qty * notional_per_unit > max_position_notional:
            approved_qty = _round_qty(max_position_notional / notional_per_unit, intent.asset_class)
            reasons.append(f"POSITION_CAPPED_TO_{approved_qty}_BY_MAX_POSITION_PCT")

        current_sector = snapshot.sector_exposure.get(intent.sector, Decimal("0"))
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


async def build_portfolio_snapshot(
    repositories: PersistenceRepositories,
    *,
    cash_balance: Decimal,
    account_equity: Decimal | None = None,
    broker_prev_close_equity: Decimal | None = None,
    mark_prices: Mapping[str, Decimal] | None = None,
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

    Note: "today" is the caller's UTC calendar day, not an NY-session
    trading day -- a known simplification versus the audited system's
    nyDayStart() handling, acceptable for MVP scope.
    """
    now = now or datetime.now(UTC)
    mark_prices = mark_prices or {}

    holding_rows = await repositories.holdings.list_all()
    holdings = [hydrate("holdings", row["payload"]) for row in holding_rows]

    holdings_value = Decimal("0")
    sector_exposure: dict[str, Decimal] = {}
    for holding in holdings:
        mark = mark_prices.get(asset_identity_key(holding.asset), holding.average_price)
        notional = abs(holding.quantity) * mark * contract_multiplier_of(holding.asset)
        holdings_value += notional
        sector = holding.sector or "Other"
        sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + notional

    intent_rows = await repositories.trade_intents.list_all(limit=1000)

    # Pending-intent notional reservation: capital already committed by an
    # approved-but-not-yet-settled TradeIntent must count as exposure too,
    # not just settled holdings -- otherwise a second, concurrent risk
    # evaluation (a different asset-class lane, say) reads the SAME stale
    # holdings_value in the window between "decision persisted" and "fill
    # settled into the holdings table" and independently approves capital
    # that's already spoken for. The RISK_APPROVED row itself IS the
    # durable reservation -- no separate ledger needed.
    _pending_notional_statuses = {
        TradeIntentStatus.RISK_APPROVED.value, TradeIntentStatus.SUBMITTED.value,
        TradeIntentStatus.ACCEPTED.value, TradeIntentStatus.PARTIALLY_FILLED.value,
        TradeIntentStatus.SUBMISSION_UNKNOWN.value,
    }
    for row in intent_rows:
        if row["status"] not in _pending_notional_statuses:
            continue
        intent = hydrate("trade_intents", row["payload"])
        # requested_quantity is overwritten with the RISK-APPROVED (possibly
        # downsized) quantity once past PROPOSED -- see execution/gateway.py's
        # replace(intent, status=RISK_APPROVED, requested_quantity=risk.approved_quantity)
        # -- so this is already the real committed size, not the original ask.
        remaining_qty = intent.requested_quantity - (intent.filled_quantity or Decimal("0"))
        if remaining_qty <= 0 or intent.reference_price is None:
            continue
        pending_notional = remaining_qty * intent.reference_price * contract_multiplier_of(intent.asset)
        holdings_value += pending_notional
        sector = intent.sector or "Other"
        sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + pending_notional

    total_equity = account_equity if account_equity and account_equity > 0 else holdings_value

    # SUBMISSION_UNKNOWN counts too -- its broker outcome is genuinely
    # unresolved (see execute_intent's _recover_unknown_submission: it may
    # still be live at the broker, and must never be blind-resubmitted), so
    # it represents the same kind of unresolved broker exposure as an
    # ordinary in-flight order. RISK_APPROVED is deliberately excluded --
    # it hasn't reached the broker yet.
    outstanding_values = {
        TradeIntentStatus.SUBMITTED.value, TradeIntentStatus.ACCEPTED.value,
        TradeIntentStatus.PARTIALLY_FILLED.value, TradeIntentStatus.SUBMISSION_UNKNOWN.value,
    }
    outstanding_orders = sum(1 for row in intent_rows if row["status"] in outstanding_values)

    fill_rows = await repositories.fills.list_all(limit=1000)
    fills = [hydrate("fills", row["payload"]) for row in fill_rows]
    today = now.date()
    trades_today = sum(1 for f in fills if f.filled_at.date() == today)

    if broker_prev_close_equity and broker_prev_close_equity > 0 and account_equity and account_equity > 0:
        daily_pnl_pct = ((account_equity - broker_prev_close_equity) / broker_prev_close_equity) * 100
        source = "broker"
    else:
        pnl_rows = await repositories.pnl_records.list_all(limit=1000)
        pnl_records = [hydrate("pnl_records", row["payload"]) for row in pnl_rows]
        daily_realized = sum((p.realized for p in pnl_records if p.as_of.date() == today), Decimal("0"))
        daily_pnl_pct = (daily_realized / total_equity) * 100 if total_equity > 0 else Decimal("0")
        source = "holdings"

    return PortfolioSnapshot(
        snapshot_id=str(uuid4()),
        as_of=now,
        total_equity=total_equity,
        cash_balance=cash_balance,
        holdings_value=holdings_value,
        sector_exposure=sector_exposure,
        open_positions=len(holdings),
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
    rows = await repositories.equity_snapshots.list_all(limit=1000)
    peak = current_equity
    for row in rows:
        equity = Decimal(str(row["payload"]["total_equity"]))
        if equity > peak:
            peak = equity
    if peak <= 0:
        return DrawdownCheck(False, None, peak, limits.max_drawdown_pct)
    drawdown_pct = ((peak - current_equity) / peak) * 100
    return DrawdownCheck(drawdown_pct >= limits.max_drawdown_pct, drawdown_pct, peak, limits.max_drawdown_pct)
