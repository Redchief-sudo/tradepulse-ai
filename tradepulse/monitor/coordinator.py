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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaPosition
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
    Holding,
    PositionLot,
    RiskLimits,
    Side,
    asset_identity_key,
    asset_key_from_broker_symbol,
    fold_price_extremum,
)
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows, run_with_lock_renewal
from tradepulse.providers import AlpacaMarketDataProvider, ProviderError
from tradepulse.strategy import atr

MonitorStatus = Literal["ok", "degraded"]


@dataclass(frozen=True, slots=True)
class MonitorCycleSummary:
    status: MonitorStatus
    positions_checked: int
    exits_triggered: int
    execution_results: list[ExecutionResult] = field(default_factory=list)
    error: str | None = None


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
    unguarded fetch here would propagate out of this module, out of
    _periodic_loop, and PERMANENTLY end this lane's scheduling
    (cli.py::_supervised_lane never restarts a lane after an unhandled
    exception) -- the position-protection safety net going dark silently
    for the rest of the run. Degrading to "skip the trailing update this
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
        # `except Exception` around classify_regime -- since _supervised_lane
        # never restarts a lane after an unhandled exception, this one
        # matters enough not to depend solely on the provider boundary
        # staying correct forever.
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


async def run_position_monitor(
    repositories: PersistenceRepositories,
    broker: AlpacaClient,
    market_data: AlpacaMarketDataProvider,
    gateway: ExecutionGateway,
    alerts: TelegramAlerter,
    risk_limits: RiskLimits,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    lease_lost: asyncio.Event | None = None,
) -> MonitorCycleSummary:
    try:
        positions = await broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - protective coverage being unavailable is itself critical
        await alerts.send("critical", f"Position protection degraded -- Alpaca positions unavailable: {exc}", {})
        return MonitorCycleSummary("degraded", 0, 0, [], error=f"BROKER_POSITIONS_UNAVAILABLE: {exc}")

    execution_results: list[ExecutionResult] = []
    exits_triggered = 0

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
    lot_rows = await paginate_all_rows(repositories.position_lots)
    open_lots_by_asset: dict[str, list[PositionLot]] = {}
    for row in lot_rows:
        lot = hydrate("position_lots", row["payload"])
        if lot.status in ("open", "partially_closed"):
            open_lots_by_asset.setdefault(asset_identity_key(lot.asset), []).append(lot)

    for position in positions:
        if lease_lost is not None and lease_lost.is_set():
            continue  # monitor's own command lease may no longer be exclusive -- stop starting new work
        holding_row = await repositories.holdings.get(asset_key_from_broker_symbol(position.asset_class, position.symbol))
        if holding_row is None:
            continue  # nothing on file (e.g. opened outside this system) -- no threshold to check
        holding = hydrate("holdings", holding_row["payload"])
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
        if holding.stop_loss is None and holding.target_price is None and not expiring and not time_stopped:
            continue
        if not _breached(position, holding) and not expiring and not time_stopped:
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
            request = ExecutionRequest(
                asset=holding.asset, side=exit_side, requested_quantity=abs(position.qty),
                strategy="position_monitor", decision_id=f"monitor-{position.symbol}-{clock().isoformat()}",
                symbol_lock_owner_token=owner_token,
            )
            result = await run_with_lock_renewal(
                database, execution_lock_key(holding.asset), owner_token, SYMBOL_LOCK_TTL_SECONDS, gateway.execute_intent(request),
            )
            execution_results.append(result)
            if result.status not in ("rejected", "skipped"):
                exits_triggered += 1
        finally:
            await release_symbol_reservation(database, holding.asset, owner_token)

    return MonitorCycleSummary("ok", len(positions), exits_triggered, execution_results)
