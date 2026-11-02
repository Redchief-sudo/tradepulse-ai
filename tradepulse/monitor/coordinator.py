"""The position monitor: protective stop/target exits for open positions,
run concurrently with (not sequentially after) the scan cycle -- see
tradepulse/cli.py.

Alpaca's own positions are the sole source of truth for quantity and current
price -- the local `holdings` table is only ever consulted for the
stop_loss/target_price thresholds, which are a TradePulse strategy decision
that only local state can know (see settlement/engine.py's
PROTECTIVE_THRESHOLD_POLICY). Every exit is submitted through the same
ExecutionGateway.execute_intent the scanner uses -- this module never talks
to the broker's order-placement endpoints directly, so the gateway remains
the sole execution boundary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter, alert_once
from tradepulse.broker import AlpacaPosition, BrokerPort
from tradepulse.broker.capabilities import AssetCapabilities, AssetTradingCapabilities, evidence_fresh
from tradepulse.dbae.policy import scale_out_feasible, whole_units_only
from tradepulse.execution import (
    SYMBOL_LOCK_TTL_SECONDS,
    ExecutionGateway,
    ExecutionRequest,
    ExecutionResult,
    execution_lock_key,
    has_in_flight_intent,
    release_symbol_reservation,
    reserve_symbol_for_execution,
)
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    Holding,
    PositionLot,
    RiskLimits,
    Side,
    asset_identity_key,
    asset_key_from_broker_symbol,
    contract_multiplier_of,
    fold_price_extremum,
)
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows, run_with_lock_renewal
from tradepulse.providers import AlpacaMarketDataProvider, ProviderError
from tradepulse.strategy import atr

logger = logging.getLogger(__name__)

MonitorStatus = Literal["ok", "degraded"]


@dataclass(frozen=True, slots=True)
class MonitorCycleSummary:
    status: MonitorStatus
    positions_checked: int
    exits_triggered: int
    execution_results: list[ExecutionResult] = field(default_factory=list)
    error: str | None = None
    unmanaged_positions: int = 0
    # Stock/option exits found while the market is closed: alerted, then
    # re-evaluated at the open (see _defer_exit_outside_market_hours).
    deferred_exits: int = 0


def _breached(position: AlpacaPosition, holding: Holding) -> bool:
    # Exit Intelligence -- current_stop (the live, ratcheted floor) is the
    # EFFECTIVE stop whenever set, overriding the static entry-derived
    # stop_loss -- never the other way around (current_stop only ever
    # moves favorably, so it's always at least as tight as stop_loss).
    effective_stop = holding.current_stop if holding.current_stop is not None else holding.stop_loss
    if position.qty > 0:
        return (effective_stop is not None and position.current_price <= effective_stop) or (
            holding.target_price is not None and position.current_price >= holding.target_price
        )
    return (effective_stop is not None and position.current_price >= effective_stop) or (
        holding.target_price is not None and position.current_price <= holding.target_price
    )


def _option_expiry_metadata_invalid(holding: Holding) -> bool:
    """True when an OPTION holding's expiry metadata is missing or
    malformed -- the near-expiry safety net (_near_expiry below) genuinely
    cannot be evaluated for it. This must be surfaced loudly (see
    run_position_monitor's critical alert), never silently folded into
    "not near expiry" -- "confirmed safe" and "unknown" are different
    things for a safety trigger, and conflating them would let the exact
    protection this trigger exists for go dark with no signal it happened.
    This should essentially never fire in practice (the scanner always sets
    valid expiry metadata) -- it's a defense against corrupted/foreign
    Holding state, not an expected runtime path."""
    if holding.asset.asset_class != AssetClass.OPTION:
        return False
    expiry_raw = holding.asset.metadata.get("expiry")
    if not expiry_raw:
        return True
    try:
        date.fromisoformat(str(expiry_raw))
    except ValueError:
        return True
    return False


def _near_expiry(holding: Holding, today: date, min_days_to_expiry: int) -> bool:
    """An independent exit trigger alongside _breached, specific to
    options: letting a long option ride to expiration risks total loss
    (or assignment, for a short leg -- moot here, this system never holds
    one) in a way no other asset class this system trades is exposed to.
    Only local state (holding.asset.metadata) knows the expiry -- Alpaca's
    position response doesn't conveniently carry it -- matching this
    module's existing division of responsibility (stop_loss/target_price
    are likewise only ever known locally, never from the broker). Returns
    False (not "near expiry") whenever _option_expiry_metadata_invalid
    would also be True -- callers must check that separately and alert;
    this function alone cannot distinguish "confirmed not near expiry"
    from "unknown," which is exactly why that separate check exists."""
    if holding.asset.asset_class != AssetClass.OPTION:
        return False
    expiry_raw = holding.asset.metadata.get("expiry")
    if not expiry_raw:
        return False
    try:
        expiry = date.fromisoformat(str(expiry_raw))
    except ValueError:
        return False
    return (expiry - today).days <= min_days_to_expiry


async def _fold_lot_extrema(repositories: PersistenceRepositories, lots: list[PositionLot], price: Decimal) -> None:
    """Outcome Attribution -- folds this cycle's broker-observed price into
    every OPEN/PARTIALLY_CLOSED lot for one asset's running mfe_price/
    mae_price (see models/portfolio.py::fold_price_extremum). Pure
    observability: never reads back into _breached or any exit decision.
    Uses RecordRepository.mutate() (atomic read-decide-write), not a plain
    get()-then-update(), because position_lots now has two concurrent
    writers -- this monitor and settlement's _project_lot, running on
    independent asyncio lanes (see cli.py::_run_trading_supervisor)."""
    for lot in lots:
        def _decide(current: PositionLot, price: Decimal = price) -> PositionLot | None:
            if current.status not in ("open", "partially_closed"):
                return None  # closed by a concurrent settlement event since the pre-fetch snapshot -- nothing to fold
            mfe, mae = fold_price_extremum(current.position_side, current.mfe_price, current.mae_price, price)
            if mfe == current.mfe_price and mae == current.mae_price:
                return None  # no-op -- neither extremum moved
            return replace(current, mfe_price=mfe, mae_price=mae)
        await repositories.position_lots.mutate(lot.lot_id, _decide)


async def _fetch_atr(market_data: AlpacaMarketDataProvider, asset: AssetIdentity) -> Decimal | None:
    """A 30-day lookback -- ATR(14) needs no more, versus the scanner's
    200-day default sized for regime/composite scoring. None on ANY
    failure (ProviderError, insufficient history, or atr() itself
    returning None) -- see run_position_monitor's own docstring note: an
    unguarded fetch here would propagate out of this module and out of
    _periodic_loop, failing the whole monitor lane; cli.py::_supervised_lane
    restarts it with capped backoff, but every position would go unchecked
    until then. Degrading to "skip the trailing update this
    cycle, keep whatever stop is already in force" is the only acceptable
    failure mode here."""
    from decimal import InvalidOperation

    try:
        candles = await market_data.fetch_candles(asset, lookback_days=30)
    except ProviderError:
        return None
    except (ValueError, InvalidOperation):
        # Defense-in-depth, not the primary guard anymore: fetch_candles
        # itself now normalizes a malformed numeric bar field or a
        # semantically invalid bar (e.g. high < low) into ProviderDataFailure
        # (a ProviderError), so the except ProviderError clause above should
        # already catch this in practice. Kept as a second layer -- same
        # redundant-guard precedent as _classify_lane_regime's own bare
        # `except Exception` around classify_regime -- since a lane failure
        # leaves every position unchecked until _supervised_lane's backoff
        # restart, this one matters enough not to depend solely on the
        # provider boundary staying correct forever.
        return None
    value = atr([float(c.high) for c in candles], [float(c.low) for c in candles], [float(c.close) for c in candles])
    return Decimal(str(value)) if value is not None else None


async def _ratchet_stop(
    repositories: PersistenceRepositories, risk_limits: RiskLimits, market_data: AlpacaMarketDataProvider,
    holding: Holding, lots: list[PositionLot], price: Decimal,
) -> Holding | None:
    """Exit Intelligence -- break-even ratchet + ATR trailing stop, both
    monotonic (favorable-direction-only). Returns the updated Holding (or
    the original, unchanged, if there was nothing to do), or None if the
    holding vanished (closed concurrently by settlement) since it was read.
    No candle fetch, no write, for the common case of a position that
    hasn't earned break-even yet -- see _fetch_atr's own cost-bounding note."""
    if holding.stop_loss is None:
        return holding  # not under TradePulse protective management -- ratchet never invents a stop from nothing
    is_long = holding.quantity > 0
    gain_pct = (
        (price - holding.average_price) / holding.average_price * 100 if is_long
        else (holding.average_price - price) / holding.average_price * 100
    )
    if holding.current_stop is None and gain_pct < risk_limits.break_even_trigger_pct:
        return holding  # break-even not yet earned this cycle

    candidate = holding.average_price  # the break-even floor, once earned, is never given back
    # ATR trailing is equity/crypto only -- options never get an ATR trail,
    # matching the existing entry-time precedent (scanner/coordinator.py's
    # options branch uses a flat pct-of-premium stop, never ATR: a contract's
    # own candle history is too short-lived/decay-driven to be a meaningful
    # momentum signal, and Alpaca's stocks-bars endpoint doesn't even serve
    # option contracts). Break-even alone still applies to options.
    if holding.asset.asset_class != AssetClass.OPTION:
        extremes = [lot.mfe_price for lot in lots if lot.mfe_price is not None]
        running_extreme = (max(extremes) if is_long else min(extremes)) if extremes else None
        if running_extreme is not None and risk_limits.trailing_atr_multiplier > 0:
            atr_value = await _fetch_atr(market_data, holding.asset)
            if atr_value is not None:
                distance = atr_value * risk_limits.trailing_atr_multiplier
                trail = running_extreme - distance if is_long else running_extreme + distance
                candidate = max(candidate, trail) if is_long else min(candidate, trail)

    def _decide(current: Holding) -> Holding | None:
        if current.current_stop is None:
            merged = candidate
        else:
            merged = max(current.current_stop, candidate) if is_long else min(current.current_stop, candidate)
        if merged == current.current_stop:
            return None  # already at least this favorable -- no-op, matches _fold_lot_extrema's convention
        return replace(current, current_stop=merged)

    return await repositories.holdings.mutate(asset_identity_key(holding.asset), _decide)


RESIDUAL_MINIMUM_VALUE = Decimal(1)  # spec §8.5: the entry minimum a held position can fall below


@dataclass(frozen=True, slots=True)
class LadderExit:
    """One exit-ladder decision: sell `quantity` for `reason`. `scale_out`
    marks the partial profit-take that also moves the stop to break-even."""

    quantity: Decimal
    reason: str
    scale_out: bool = False


def _scale_out_quantity(holding: Holding, position_qty: Decimal, price: Decimal, risk_limits: RiskLimits,
                        asset_caps: AssetTradingCapabilities | None) -> Decimal | None:
    """The partial-exit quantity, or None when the position cannot be split:
    a single option contract, either part below the order precision or the
    minimum lot notional, or (spec §8.4) a part that is not feasible for the
    asset's own minimum and increment. An asset not verified fractionable
    (``asset_caps`` None is unknown evidence) is split in whole units only:
    the part is floored to whole shares, and both parts must be whole.
    Unknown crypto evidence is not split."""
    from tradepulse.risk.engine import round_quantity

    asset_class = holding.asset.asset_class
    part = round_quantity(position_qty * risk_limits.scale_out_fraction, asset_class)
    if whole_units_only(asset_caps, asset_class):
        part = part.to_integral_value(rounding=ROUND_FLOOR)
    rest = position_qty - part
    notional_per_unit = price * contract_multiplier_of(holding.asset)
    if part <= 0 or rest <= 0:
        return None
    if part * notional_per_unit < risk_limits.min_lot_notional or rest * notional_per_unit < risk_limits.min_lot_notional:
        return None
    if not scale_out_feasible(asset_caps, asset_class, contract_multiplier_of(holding.asset), part, rest, price):
        return None
    return part


def _exit_reason(position: AlpacaPosition, holding: Holding, *, breached: bool, expiring: bool,
                 time_stopped: bool, ladder: LadderExit | None) -> str:
    """Why the monitor is exiting, in the order the decision was made."""
    if breached:
        is_long = position.qty > 0
        if holding.target_price is not None and (
                position.current_price >= holding.target_price if is_long else position.current_price <= holding.target_price):
            return "target_price"
        if holding.current_stop is not None and holding.stop_loss is not None and holding.current_stop != holding.stop_loss:
            return "break_even_stop" if holding.current_stop == holding.average_price else "trailing_stop"
        return "stop_loss"
    if expiring:
        return "option_expiry"
    if time_stopped:
        return "time_stop"
    return ladder.reason if ladder is not None else "other"


def plan_ladder_exit(holding: Holding, lots: list[PositionLot], position_qty: Decimal, price: Decimal,
                     today: date, risk_limits: RiskLimits,
                     asset_caps: AssetTradingCapabilities | None = None) -> LadderExit | None:
    """Profit-aware exits measured in R, the position's own risk per unit
    (average_price - stop_loss, the governing first-entry stop). Long
    positions only -- TradePulse never opens a short.

    - final_target_r: exit everything.
    - Not yet scaled out and splittable: at scale_out_r, sell
      scale_out_fraction (the caller first moves the stop to break-even).
    - Not splittable: exit everything at single_unit_target_r.
    - Not yet scaled out and held stall_exit_days without its best price
      ever reaching stall_min_r: exit everything; the trade is not working.

    Stops, the trailing stop, option expiry and max_hold_days are decided
    before this and take precedence."""
    if holding.stop_loss is None or holding.quantity <= 0 or position_qty <= 0:
        return None
    risk_unit = holding.average_price - holding.stop_loss
    if risk_unit <= 0:
        return None
    gain_r = (price - holding.average_price) / risk_unit
    if risk_limits.final_target_r and gain_r >= risk_limits.final_target_r:
        return LadderExit(position_qty, "target_r")
    if holding.scaled_out:
        return None
    part = _scale_out_quantity(holding, position_qty, price, risk_limits, asset_caps)
    if part is None:
        if risk_limits.single_unit_target_r and gain_r >= risk_limits.single_unit_target_r:
            return LadderExit(position_qty, "target_r_single_unit")
    elif risk_limits.scale_out_r and gain_r >= risk_limits.scale_out_r:
        return LadderExit(part, "scale_out", scale_out=True)
    if risk_limits.stall_exit_days and lots:
        oldest = min(lots, key=lambda lot: lot.opened_at)
        if (today - oldest.opened_at.date()).days >= risk_limits.stall_exit_days:
            extremes = [lot.mfe_price for lot in lots if lot.mfe_price is not None]
            best = max([price, *extremes])
            if (best - holding.average_price) / risk_unit < risk_limits.stall_min_r:
                return LadderExit(position_qty, "stall")
    return None


def _time_stopped(lots: list[PositionLot], today: date, max_hold_days: int) -> bool:
    """Deliberately stateless and NOT preserved across a governing-lot
    change -- recomputed fresh every cycle from whichever lot is currently
    oldest, exactly matching stop_loss/target_price's own already-accepted
    "re-source from new oldest lot" behavior at the same event
    (PROTECTIVE_THRESHOLD_POLICY). No new inconsistency introduced."""
    if max_hold_days <= 0 or not lots:
        return False  # 0 disables -- mirrors atr_stop_multiplier's existing "0 = off" convention
    oldest_lot = min(lots, key=lambda lot: lot.opened_at)
    return (today - oldest_lot.opened_at.date()).days >= max_hold_days


def _scale_out_possible(holding: Holding, price: Decimal, risk_limits: RiskLimits) -> bool:
    """True when plan_ladder_exit could choose a scale-out this cycle, the only
    decision that reads asset capabilities (spec §8.4)."""
    if holding.scaled_out or not risk_limits.scale_out_r or holding.stop_loss is None:
        return False
    risk_unit = holding.average_price - holding.stop_loss
    return risk_unit > 0 and (price - holding.average_price) / risk_unit >= risk_limits.scale_out_r


async def _read_asset_caps(capabilities: AssetCapabilities, asset: AssetIdentity, now: datetime) -> AssetTradingCapabilities | None:
    """The asset's capability evidence, or None (unknown) when the read fails
    or the evidence is stale (spec §3.2, §8.4)."""
    try:
        caps = await capabilities.get_asset_capabilities(asset)
    except Exception as exc:  # noqa: BLE001 - unknown evidence only limits a scale-out, never an exit
        logger.warning("monitor_asset_capabilities_unavailable symbol=%s error=%s", asset.symbol, type(exc).__name__)
        return None
    return caps if evidence_fresh(caps.received_at, now) else None


async def _report_residual_below_minimum(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                                         position: AlpacaPosition, holding: Holding, now: datetime) -> None:
    """Spec §8.5: a held position worth under $1 is a warning, once per asset
    per session date. It never changes the exit decision (the protective
    exit is still a full-quantity sell) and never raises into the exit loop:
    it runs ahead of the stop check, so everything, the value included (an
    option holding with no contract multiplier raises), is inside the guard."""
    from tradepulse.calendar import CalendarHorizonExceeded, market_calendar, ny_date
    from tradepulse.dbae.baseline import session_date_for
    from tradepulse.reconciliation.coordinator import _alert_once

    try:
        value = abs(position.qty) * position.current_price * contract_multiplier_of(holding.asset)
        if value >= RESIDUAL_MINIMUM_VALUE:
            return
        try:
            window = session_date_for(market_calendar(ny_date(now)), now)
        except (CalendarHorizonExceeded, ValueError):
            window = ny_date(now)  # no session placed: the New York date still bounds the alert to once per day
        key = asset_identity_key(holding.asset)
        await _alert_once(
            repositories, alerts, event_type="DBAE_RESIDUAL_BELOW_ENTRY_MINIMUM", subject=key, window=window.isoformat(),
            severity="warning",
            message=(f"DBAE_RESIDUAL_BELOW_ENTRY_MINIMUM: {holding.asset.symbol} position worth {value} is below the $1 "
                     "entry minimum; its protective exit is still a full-quantity sell."),
            details={"asset_key": key, "market_value": str(value), "quantity": str(position.qty)}, now=now)
    except Exception:  # an alert failure must never block a protective exit
        logger.exception("monitor_residual_alert_failed symbol=%s", position.symbol)


async def _report_unmanaged_position(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                                     position: AlpacaPosition, now: datetime) -> None:
    """A broker position with no local holding has no stop, target or time
    stop. One critical audit event per asset per UTC day (deterministic id),
    recorded whether or not Telegram is reachable, and its alert delivered
    once (see alert_once)."""
    key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
    event_id = f"unmanaged_position:{key}:{now.date().isoformat()}"
    event = AuditEvent(
        event_id=event_id, event_type="unmanaged_broker_position", severity="critical",
        message=(f"UNMANAGED_POSITION: {position.symbol} qty {position.qty} has no local holding -- "
                 "no stop, target or time stop protects it."),
        occurred_at=now, entity_type="broker_position", entity_id=key,
        details={"symbol": position.symbol, "asset_class": position.asset_class.value, "qty": str(position.qty)},
    )
    await alert_once(repositories.audit_events, alerts, event)


async def _untradeable_assets(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                              positions: list[AlpacaPosition], now: datetime) -> set[str]:
    """Rev.140 §3.4: assets the monitor must not trade at all -- a negative
    broker position, open quarantined inventory, or an open unresolved
    execution. The first two are alerted once per record per UTC day; an
    unresolved execution is reconciliation's to alert."""
    from tradepulse.quarantine import quarantined_positions
    from tradepulse.reconciliation.unresolved import open_unresolved

    untradeable: set[str] = set()
    day = now.date().isoformat()
    for record in await quarantined_positions(repositories):
        untradeable.add(record.asset_key)
        await alert_once(repositories.audit_events, alerts, AuditEvent(
            event_id=f"quarantined_inventory:{record.record_id}:{day}", event_type="quarantined_inventory", severity="critical",
            message=(f"QUARANTINED_INVENTORY: {record.asset_key} holds {record.open_quantity} quarantined ({record.kind}, "
                     f"{record.record_id}); TradePulse places no order on it until it is resolved."),
            occurred_at=now, entity_type="quarantined_inventory", entity_id=record.record_id,
            details={"record_id": record.record_id, "asset_key": record.asset_key, "open_quantity": str(record.open_quantity)}))
    for position in positions:
        key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
        if position.qty < 0:
            untradeable.add(key)
            await alert_once(repositories.audit_events, alerts, AuditEvent(
                event_id=f"unsupported_short_position:{key}:{day}", event_type="unsupported_short_position", severity="critical",
                message=(f"UNSUPPORTED_SHORT_POSITION: {position.symbol} qty {position.qty} at the broker; "
                         "TradePulse never trades a short position."),
                occurred_at=now, entity_type="broker_position", entity_id=key,
                details={"symbol": position.symbol, "qty": str(position.qty)}))
    untradeable |= {row.asset_key for row in await open_unresolved(repositories)}
    return untradeable


async def _equity_market_open(broker: BrokerPort) -> bool | None:
    """Alpaca's clock for the US stock/option session; None if unavailable."""
    try:
        return (await broker.get_clock()).is_open
    except Exception:  # noqa: BLE001 - an unknown session must not stop the monitor
        logger.warning("monitor_market_clock_unavailable", exc_info=True)
        return None


async def _defer_exit_outside_market_hours(repositories: PersistenceRepositories, alerts: TelegramAlerter,
                                           position: AlpacaPosition, holding: Holding, now: datetime) -> bool:
    """Stock and option exits act on regular-session prices only. Outside
    the session Alpaca's position price keeps moving with overnight trading,
    but no executable quote exists, so an exit cannot be placed and a thin
    overnight print is not a reason to sell. A breach seen then is recorded
    and alerted once per symbol per UTC day; the next in-session cycle exits
    if the price is still beyond the stop. Returns True when a breach was seen."""
    if (holding.stop_loss is None and holding.target_price is None) or not _breached(position, holding):
        return False
    key = asset_identity_key(holding.asset)
    stop = holding.current_stop if holding.current_stop is not None else holding.stop_loss
    event = AuditEvent(
        event_id=f"exit_deferred_market_closed:{key}:{now.date().isoformat()}", event_type="exit_deferred_market_closed",
        severity="warning",
        message=(f"EXIT_DEFERRED_MARKET_CLOSED: {position.symbol} at {position.current_price} is past its exit level "
                 f"(stop {stop}, target {holding.target_price}) outside regular market hours. It will be sold at the "
                 "open if it is still past it."),
        occurred_at=now, entity_type="holding", entity_id=key,
        details={"symbol": position.symbol, "price": str(position.current_price), "stop": str(stop)},
    )
    await alert_once(repositories.audit_events, alerts, event)
    return True


async def run_position_monitor(
    repositories: PersistenceRepositories,
    broker: BrokerPort,
    market_data: AlpacaMarketDataProvider,
    gateway: ExecutionGateway,
    alerts: TelegramAlerter,
    risk_limits: RiskLimits,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    lease_lost: asyncio.Event | None = None,
    adoption: Any = None,
    capabilities: AssetCapabilities | None = None,
) -> MonitorCycleSummary:
    try:
        positions = await broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - protective coverage being unavailable is itself critical
        await alerts.send("critical", f"Position protection degraded -- Alpaca positions unavailable: {exc}", {})
        return MonitorCycleSummary("degraded", 0, 0, [], error=f"BROKER_POSITIONS_UNAVAILABLE: {exc}")

    # Rev.142 §3.4 step 2: adopt eligible unmanaged longs before anything
    # reads holdings, so a new adopted holding is protected this same cycle.
    # ``adoption`` is set only with the setting on and the startup gate passed.
    if adoption is not None:
        from tradepulse.adoption import adoption_pass
        try:
            await adoption_pass(repositories, positions, adoption, alerts, clock())
        except Exception:  # a failed adoption must never block other positions' exits
            logger.exception("monitor_adoption_pass_failed")

    # The same evidence source the gateway reads (spec §8.4); a failed or
    # stale read is unknown evidence for the scale-out, never a blocked exit.
    asset_evidence = capabilities if capabilities is not None else gateway.capabilities
    execution_results: list[ExecutionResult] = []
    exits_triggered = 0
    deferred_exits = 0
    # Stocks and options are managed on regular-session prices only; crypto
    # trades around the clock. None (clock unavailable) keeps the previous
    # behaviour: evaluate, and let the gateway refuse a stale quote.
    equity_market_open = None
    if any(p.asset_class != AssetClass.CRYPTO for p in positions):
        equity_market_open = await _equity_market_open(broker)

    # Outcome Attribution -- one pre-fetch of ALL open/partially-closed lots,
    # grouped by asset, rather than a per-position table scan. A symbol can
    # have more than one open lot (a scale-in); every open lot for a symbol
    # sees the SAME broker-observed price this cycle, just folds it against
    # its own individually-tracked extremes.
    # FIN-090-01: unbounded, whole-table pagination -- NOT list_all(limit=N),
    # which is oldest-first and would silently drop a newer open lot once
    # enough OTHER (any-asset) lots existed first, hiding it from
    # protective-stop monitoring. Open/closed is determined below via the
    # payload's own Decimal-based lot.status property, never a SQL-side
    # numeric filter.
    from tradepulse.quarantine.legacy import converted_lots

    lot_rows = await paginate_all_rows(repositories.position_lots)
    converted = await converted_lots(repositories)
    open_lots_by_asset: dict[str, list[PositionLot]] = {}
    for row in lot_rows:
        lot = hydrate("position_lots", row["payload"])
        if lot.status in ("open", "partially_closed") and lot.lot_id not in converted:
            open_lots_by_asset.setdefault(asset_identity_key(lot.asset), []).append(lot)

    # Detection pass first: an unprotected position is reported before any
    # quote fetch, ratchet or (sequential, possibly slow) exit in this cycle.
    # Detection only reports: nothing here may raise into the exit loop below.
    from tradepulse.reconciliation.membership import opening_quantities
    from tradepulse.verification.opening import load_bound_opening_checkpoint
    try:
        opening = opening_quantities(await repositories.trade_intents.database.run(load_bound_opening_checkpoint))
    except Exception:  # noqa: BLE001 - a bad verification artifact must never block protective exits
        # Opening inventory unknown: fail closed for alerting only, so every
        # unheld broker position is reported rather than skipped.
        logger.error("monitor_opening_inventory_unavailable", exc_info=True)
        opening = {}
    try:
        untradeable = await _untradeable_assets(repositories, alerts, positions, clock())
    except Exception:  # must never block other assets' exits; the gateway refuses these assets independently
        logger.exception("monitor_untradeable_check_failed")
        untradeable = set()
    unmanaged = 0
    for position in positions:
        try:
            key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
            if key in untradeable:
                continue  # reported as short or quarantined inventory instead -- never "unmanaged"
            # With adoption on (Rev.142), the opening-checkpoint suppression is
            # removed: an inherited position that failed adoption is reported too.
            if await repositories.holdings.get(key) is None and (adoption is not None or opening.get(key) != position.qty):
                unmanaged += 1
                await _report_unmanaged_position(repositories, alerts, position, clock())
        except Exception:  # noqa: BLE001 - detection failure on one position must never block exits
            logger.error("monitor_unmanaged_detection_failed symbol=%s", position.symbol, exc_info=True)

    for position in positions:
        if lease_lost is not None and lease_lost.is_set():
            continue  # monitor's own command lease may no longer be exclusive -- stop starting new work
        if asset_key_from_broker_symbol(position.asset_class, position.symbol) in untradeable:
            continue  # Rev.140 §3.4: no order of any kind on a short, quarantined or unaccounted asset
        holding_row = await repositories.holdings.get(asset_key_from_broker_symbol(position.asset_class, position.symbol))
        if holding_row is None:
            continue  # nothing on file (e.g. opened outside this system) -- no threshold to check
        holding = hydrate("holdings", holding_row["payload"])
        if holding.asset.asset_class != AssetClass.CRYPTO and equity_market_open is False:
            # No ratchet, no extreme folding, no exit on out-of-session prints.
            if await _defer_exit_outside_market_hours(repositories, alerts, position, holding, clock()):
                deferred_exits += 1
            continue
        await _report_residual_below_minimum(repositories, alerts, position, holding, clock())
        matching_lots = open_lots_by_asset.get(asset_identity_key(holding.asset), [])
        if matching_lots:
            await _fold_lot_extrema(repositories, matching_lots, position.current_price)
        ratcheted = await _ratchet_stop(repositories, risk_limits, market_data, holding, matching_lots, position.current_price)
        if ratcheted is None:
            continue  # holding vanished (closed concurrently by settlement) -- same treatment as holding_row is None above
        holding = ratcheted
        if _option_expiry_metadata_invalid(holding):
            await alerts.send(
                "critical",
                f"OPTION_EXPIRY_METADATA_INVALID for {position.symbol} -- the near-expiry forced-close safety check "
                "cannot run for this position; its stop/target protection (if any) is unaffected.",
                {"symbol": position.symbol, "asset_class": holding.asset.asset_class.value},
            )
        expiring = _near_expiry(holding, clock().date(), risk_limits.options_forced_close_days_before_expiry)
        time_stopped = _time_stopped(matching_lots, clock().date(), risk_limits.max_hold_days)
        breached = (holding.stop_loss is not None or holding.target_price is not None) and _breached(position, holding)
        asset_caps = (None if breached or expiring or time_stopped
                      or not _scale_out_possible(holding, position.current_price, risk_limits)
                      else await _read_asset_caps(asset_evidence, holding.asset, clock()))
        ladder = (None if breached or expiring or time_stopped
                  else plan_ladder_exit(holding, matching_lots, abs(position.qty), position.current_price,
                                        clock().date(), risk_limits, asset_caps))
        if not (breached or expiring or time_stopped or ladder):
            continue

        database = repositories.trade_intents.database
        owner_token = str(uuid4())
        if not await reserve_symbol_for_execution(database, holding.asset, owner_token):
            continue  # another coordinator is already processing this asset -- don't race it
        try:
            if await has_in_flight_intent(repositories, holding.asset):
                continue  # don't fight an order already in flight on this symbol (e.g. from the scanner)

            is_long = position.qty > 0
            exit_side = Side.SELL if is_long else Side.BUY
            if ladder is not None and ladder.scale_out:
                # Lock the stop at break-even and record the scale-out BEFORE
                # selling: if the sale then fails, the position is still
                # protected and is never scaled out twice.
                def _mark(current: Holding) -> Holding | None:
                    if current.scaled_out:
                        return None
                    floor = current.average_price if current.current_stop is None else max(current.current_stop, current.average_price)
                    return replace(current, scaled_out=True, current_stop=floor)
                marked = await repositories.holdings.mutate(asset_identity_key(holding.asset), _mark)
                if marked is None or not marked.scaled_out:
                    continue  # holding gone, or another writer already scaled it out
            request = ExecutionRequest(
                asset=holding.asset, side=exit_side,
                requested_quantity=ladder.quantity if ladder is not None else abs(position.qty),
                strategy="position_monitor",
                decision_id=f"monitor-{ladder.reason if ladder else 'exit'}-{position.symbol}-{clock().isoformat()}",
                symbol_lock_owner_token=owner_token,
                exit_reason=_exit_reason(position, holding, breached=breached, expiring=expiring,
                                         time_stopped=time_stopped, ladder=ladder),
            )
            result = await run_with_lock_renewal(
                database, execution_lock_key(holding.asset), owner_token, SYMBOL_LOCK_TTL_SECONDS, gateway.execute_intent(request),
            )
            execution_results.append(result)
            if result.status == "rejected" and any(lot.origin == "adopted" for lot in matching_lots):
                # Rev.142 §3.5: the adoption and holding stay; the refusal is an incident.
                key = asset_identity_key(holding.asset)
                await alert_once(repositories.audit_events, alerts, AuditEvent(
                    event_id=f"adopted_exit_refused:{key}:{clock().strftime('%Y-%m-%dT%H')}", event_type="adopted_exit_refused",
                    severity="critical", occurred_at=clock(), entity_type="asset", entity_id=key,
                    message=f"ADOPTED_EXIT_REFUSED: {holding.asset.symbol} exit refused ({', '.join(result.reasons)}); "
                            "the adopted position stays protected and is retried.",
                    details={"asset_key": key, "reasons": list(result.reasons)}))
            if ladder is not None and ladder.scale_out and result.status in ("rejected", "skipped") and not result.filled_quantity:
                # Nothing was sold (a stale quote at the open, say): clear the
                # scale-out mark so the next cycle retries. The break-even stop
                # set with it stays.
                def _unmark(current: Holding) -> Holding | None:
                    return replace(current, scaled_out=False) if current.scaled_out else None
                await repositories.holdings.mutate(asset_identity_key(holding.asset), _unmark)
            if result.status not in ("rejected", "skipped"):
                exits_triggered += 1
        finally:
            await release_symbol_reservation(database, holding.asset, owner_token)

    return MonitorCycleSummary("ok", len(positions), exits_triggered, execution_results, unmanaged_positions=unmanaged,
                               deferred_exits=deferred_exits)
