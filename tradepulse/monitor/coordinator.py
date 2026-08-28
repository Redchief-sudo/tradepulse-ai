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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
from tradepulse.models import AssetClass, Holding, RiskLimits, Side, asset_key_from_broker_symbol
from tradepulse.persistence import PersistenceRepositories, hydrate, run_with_lock_renewal

MonitorStatus = Literal["ok", "degraded"]


@dataclass(frozen=True, slots=True)
class MonitorCycleSummary:
    status: MonitorStatus
    positions_checked: int
    exits_triggered: int
    execution_results: list[ExecutionResult] = field(default_factory=list)
    error: str | None = None


def _breached(position: AlpacaPosition, holding: Holding) -> bool:
    if position.qty > 0:
        return (holding.stop_loss is not None and position.current_price <= holding.stop_loss) or (
            holding.target_price is not None and position.current_price >= holding.target_price
        )
    return (holding.stop_loss is not None and position.current_price >= holding.stop_loss) or (
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


async def run_position_monitor(
    repositories: PersistenceRepositories,
    broker: AlpacaClient,
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

    for position in positions:
        if lease_lost is not None and lease_lost.is_set():
            continue  # monitor's own command lease may no longer be exclusive -- stop starting new work
        holding_row = await repositories.holdings.get(asset_key_from_broker_symbol(position.asset_class, position.symbol))
        if holding_row is None:
            continue  # nothing on file (e.g. opened outside this system) -- no threshold to check
        holding = hydrate("holdings", holding_row["payload"])
        if _option_expiry_metadata_invalid(holding):
            await alerts.send(
                "critical",
                f"OPTION_EXPIRY_METADATA_INVALID for {position.symbol} -- the near-expiry forced-close safety check "
                "cannot run for this position; its stop/target protection (if any) is unaffected.",
                {"symbol": position.symbol, "asset_class": holding.asset.asset_class.value},
            )
        expiring = _near_expiry(holding, clock().date(), risk_limits.options_forced_close_days_before_expiry)
        if holding.stop_loss is None and holding.target_price is None and not expiring:
            continue
        if not _breached(position, holding) and not expiring:
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
