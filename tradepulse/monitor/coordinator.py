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

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient, AlpacaPosition
from tradepulse.execution import ExecutionGateway, ExecutionRequest, ExecutionResult, has_in_flight_intent
from tradepulse.models import Holding, Side
from tradepulse.persistence import PersistenceRepositories, hydrate

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


async def run_position_monitor(
    repositories: PersistenceRepositories,
    broker: AlpacaClient,
    gateway: ExecutionGateway,
    alerts: TelegramAlerter,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MonitorCycleSummary:
    try:
        positions = await broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - protective coverage being unavailable is itself critical
        await alerts.send("critical", f"Position protection degraded -- Alpaca positions unavailable: {exc}", {})
        return MonitorCycleSummary("degraded", 0, 0, [], error=f"BROKER_POSITIONS_UNAVAILABLE: {exc}")

    execution_results: list[ExecutionResult] = []
    exits_triggered = 0

    for position in positions:
        holding_row = await repositories.holdings.get(position.symbol.upper())
        if holding_row is None:
            continue  # nothing on file (e.g. opened outside this system) -- no threshold to check
        holding = hydrate("holdings", holding_row["payload"])
        if holding.stop_loss is None and holding.target_price is None:
            continue
        if not _breached(position, holding):
            continue
        if await has_in_flight_intent(repositories, position.symbol):
            continue  # don't fight an order already in flight on this symbol (e.g. from the scanner)

        is_long = position.qty > 0
        exit_side = Side.SELL if is_long else Side.BUY
        request = ExecutionRequest(
            asset=holding.asset, side=exit_side, requested_quantity=abs(position.qty),
            strategy="position_monitor", decision_id=f"monitor-{position.symbol}-{clock().isoformat()}",
        )
        result = await gateway.execute_intent(request)
        execution_results.append(result)
        if result.status not in ("rejected", "skipped"):
            exits_triggered += 1

    return MonitorCycleSummary("ok", len(positions), exits_triggered, execution_results)
