"""Single canonical path for turning validated Alpaca FILL activities into
local Fill/SettlementEvent records -- shared by ExecutionGateway._poll_and_settle
(live, bounded polling right after submission) and reconciliation's late-fill
recovery (one-shot, run later against an order the live poll window already
gave up on). There must be exactly one way a Fill/SettlementEvent gets
created, matching this system's single-execution-path principle (see
execution/gateway.py's module docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaActivity, AlpacaClient, AlpacaError
from tradepulse.models import Fill, SettlementEvent, TradeIntent, TradeIntentStatus
from tradepulse.persistence import PersistenceRepositories
from tradepulse.settlement import SettlementProcessor

TERMINAL_STATUSES = frozenset(
    {TradeIntentStatus.FILLED, TradeIntentStatus.REJECTED, TradeIntentStatus.CANCELED, TradeIntentStatus.EXPIRED, TradeIntentStatus.FAILED}
)
TERMINAL_FAILURE_ORDER_STATUSES = frozenset({"rejected", "canceled", "expired", "replaced"})
TERMINAL_ORDER_STATUSES = frozenset({"filled", "done_for_day"} | TERMINAL_FAILURE_ORDER_STATUSES)


def _is_valid_fill_activity(activity: AlpacaActivity, current: TradeIntent) -> bool:
    """A broker activity becoming a financial ledger record is an
    accounting boundary -- validate every available field before
    admitting it, rather than trusting order_id alone."""
    if not activity.activity_id:
        return False
    if activity.symbol != current.asset.symbol:
        return False
    if activity.side != current.side:
        return False
    if activity.qty is None or activity.qty <= 0:
        return False
    if activity.price is None or activity.price <= 0:
        return False
    if activity.transaction_time is None:
        return False
    return True


async def attribute_order_fills(
    repositories: PersistenceRepositories, broker: AlpacaClient, alerts: TelegramAlerter,
    current: TradeIntent, clock: Callable[[], datetime],
) -> Decimal:
    """Fetch this order's real FILL activities from Alpaca and create a
    local Fill/SettlementEvent for each validated one not already recorded,
    keyed by Alpaca's own activity_id -- the real broker fill identity, not
    a locally-synthesized string. Returns the total quantity attributed
    from validated activities so far, so the caller can tell whether the
    Activities API has fully caught up with what /orders reports."""
    activities = await broker.get_activities(activity_type="FILL", since=current.created_at)
    order_activities = [a for a in activities if str(a.raw.get("order_id") or "") == current.broker_order_id]

    validated: list[AlpacaActivity] = []
    for activity in order_activities:
        if not _is_valid_fill_activity(activity, current):
            await alerts.send(
                "critical",
                f"BROKER_FILL_INTEGRITY_MISMATCH: Alpaca FILL activity {activity.activity_id} is linked to "
                f"order {current.broker_order_id} but its symbol/side/qty/price/timestamp don't match the "
                f"expected trade -- excluded from attribution, not recorded as a Fill.",
                {"trade_intent_id": current.trade_intent_id, "broker_order_id": current.broker_order_id, "activity_id": activity.activity_id},
            )
            continue
        validated.append(activity)
    validated.sort(key=lambda a: (a.transaction_time, a.activity_id))

    for activity in validated:
        fill = Fill(
            fill_id=activity.activity_id, trade_intent_id=current.trade_intent_id, order_id=current.broker_order_id,
            asset=current.asset, side=current.side, execution_mode=current.execution_mode,
            quantity=activity.qty, price=activity.price, fees=Decimal("0"), slippage=Decimal("0"),
            filled_at=activity.transaction_time, broker_fill_id=activity.activity_id,
        )
        created = await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.fill_id)
        if created:
            event = SettlementEvent(
                settlement_event_id=fill.fill_id, fill_id=fill.fill_id, trade_intent_id=current.trade_intent_id,
                asset=current.asset, side=current.side, execution_mode=current.execution_mode,
                quantity=fill.quantity, price=fill.price, occurred_at=activity.transaction_time,
                broker_order_id=current.broker_order_id, broker_fill_id=activity.activity_id,
                client_order_id=current.client_order_id, sector=current.sector,
            )
            await repositories.settlements.create_once(fill.fill_id, event, status=event.status.value, unique_value=fill.fill_id)

    return sum((a.qty for a in validated), Decimal("0"))


async def resolve_order_from_broker(
    repositories: PersistenceRepositories, broker: AlpacaClient, settlement: SettlementProcessor,
    alerts: TelegramAlerter, intent: TradeIntent, clock: Callable[[], datetime],
) -> Decimal:
    """One-shot recovery for an order the live poll window already gave up
    on: attribute whatever validated fill activities exist right now via
    attribute_order_fills (safe and idempotent regardless of intent status),
    and if the intent isn't already terminal, check the order's CURRENT
    broker status and finalize it to FILLED/PARTIALLY_FILLED/REJECTED --
    mirrors _poll_and_settle's terminal-status branch as a single pass, no
    polling loop or timeout. Returns the total attributed quantity."""
    if intent.broker_order_id is None:
        return Decimal("0")
    attributed_qty = await attribute_order_fills(repositories, broker, alerts, intent, clock)
    if intent.status in TERMINAL_STATUSES:
        return attributed_qty  # already finalized -- the new Fill/SettlementEvent will be
        # picked up by the normal `tradepulse settle` cadence
    try:
        order = await broker.get_order(intent.broker_order_id)
    except AlpacaError:
        return attributed_qty

    cumulative_filled = order.filled_qty
    if attributed_qty > cumulative_filled:
        await alerts.send(
            "critical",
            f"BROKER_FILL_INTEGRITY_MISMATCH: attributed fill quantity ({attributed_qty}) exceeds order.filled_qty "
            f"({cumulative_filled}) for {intent.asset.symbol} order {intent.broker_order_id} during late-fill recovery.",
            {"trade_intent_id": intent.trade_intent_id, "broker_order_id": intent.broker_order_id},
        )
        return attributed_qty

    if order.status in TERMINAL_ORDER_STATUSES and attributed_qty >= cumulative_filled:
        terminal_status = (
            (TradeIntentStatus.PARTIALLY_FILLED if attributed_qty > 0 else TradeIntentStatus.REJECTED)
            if order.status in TERMINAL_FAILURE_ORDER_STATUSES else TradeIntentStatus.FILLED
        )
        updated = replace(intent, status=terminal_status, filled_quantity=attributed_qty, filled_avg_price=order.filled_avg_price or intent.filled_avg_price)
        await repositories.trade_intents.update(intent.trade_intent_id, updated, status=updated.status.value)
        await settlement.process_pending()
    elif attributed_qty != intent.filled_quantity:
        updated = replace(intent, filled_quantity=attributed_qty, status=TradeIntentStatus.PARTIALLY_FILLED if attributed_qty > 0 else intent.status)
        await repositories.trade_intents.update(intent.trade_intent_id, updated, status=updated.status.value)

    return attributed_qty


__all__ = [
    "TERMINAL_FAILURE_ORDER_STATUSES",
    "TERMINAL_ORDER_STATUSES",
    "TERMINAL_STATUSES",
    "attribute_order_fills",
    "resolve_order_from_broker",
]
