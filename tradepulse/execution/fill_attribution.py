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
from dataclasses import dataclass, replace
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

# The local TradeIntentStatus each broker failure status maps to when NO
# quantity was actually attributed -- distinct enum values, not a single
# generic REJECTED, so the persisted state reflects Alpaca's own vocabulary.
# "replaced" (this codebase never issues a replace-order request itself,
# but a terminal order could still arrive in this state) has no closer
# existing match than CANCELED: the order was superseded, not "rejected".
_ZERO_FILL_TERMINAL_STATUS: dict[str, TradeIntentStatus] = {
    "rejected": TradeIntentStatus.REJECTED,
    "canceled": TradeIntentStatus.CANCELED,
    "expired": TradeIntentStatus.EXPIRED,
    "replaced": TradeIntentStatus.CANCELED,
}


def terminal_status_for_order(
    order_status: str, attributed_qty: Decimal, requested_quantity: Decimal | None,
) -> TradeIntentStatus | None:
    """Maps a terminal broker order status to the correct local
    TradeIntentStatus -- driven by exact attributed fill quantity against
    what was actually requested, never by the order-status string alone.
    Callers must only invoke this once attributed_qty is known to fully
    reflect the broker's currently-reported filled quantity for this order
    (no attribution lag remaining -- see _poll_and_settle/resolve_order_from_broker,
    both of which already guarantee attributed_qty == order.filled_qty by
    the time they call this).

    Returns None when the broker status is genuinely inconclusive about
    final disposition -- the caller must leave the intent non-terminal and
    let a later poll/reconciliation pass revisit it, exactly like the
    existing attribution-lag handling, never force one of the two cases
    below into a status that isn't actually true yet:
      - a `done_for_day` order with zero fills today -- Alpaca may still
        send further updates the next trading day, so this is not
        equivalent to a permanent cancellation;
      - a `filled` order whose attributed quantity is less than what was
        requested -- a broker-side contradiction (Alpaca's `filled` always
        means the order's own full submitted quantity was executed), not
        evidence of a genuine partial fill."""
    if requested_quantity is not None and requested_quantity > 0 and attributed_qty >= requested_quantity:
        return TradeIntentStatus.FILLED
    if order_status in TERMINAL_FAILURE_ORDER_STATUSES:
        if attributed_qty > 0:
            return TradeIntentStatus.PARTIALLY_FILLED
        return _ZERO_FILL_TERMINAL_STATUS.get(order_status, TradeIntentStatus.REJECTED)
    if order_status == "done_for_day":
        return TradeIntentStatus.PARTIALLY_FILLED if attributed_qty > 0 else None
    return None  # order_status == "filled" but attributed_qty hasn't reached requested_quantity


@dataclass(frozen=True, slots=True)
class AttributedFills:
    """quantity and avg_price both derive from the SAME validated activity
    evidence -- never mix an attributed quantity with a broker-reported
    average price covering more (or different) fills than were actually
    attributed. avg_price is None only when quantity is zero."""

    quantity: Decimal
    avg_price: Decimal | None


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
) -> AttributedFills:
    """Fetch this order's real FILL activities from Alpaca and create a
    local Fill/SettlementEvent for each validated one not already recorded,
    keyed by Alpaca's own activity_id -- the real broker fill identity, not
    a locally-synthesized string. Returns the total quantity AND average
    price attributed from validated activities so far -- both computed from
    the same evidence, so a caller never pairs an attributed quantity with
    a broker-reported average price that covers more (or different) fills
    than were actually attributed. The caller can compare `.quantity`
    against /orders' cumulative filled_qty to tell whether the Activities
    API has fully caught up."""
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

    total_qty = sum((a.qty for a in validated), Decimal("0"))
    total_notional = sum((a.qty * a.price for a in validated), Decimal("0"))
    avg_price = total_notional / total_qty if total_qty > 0 else None
    return AttributedFills(quantity=total_qty, avg_price=avg_price)


async def resolve_order_from_broker(
    repositories: PersistenceRepositories, broker: AlpacaClient, settlement: SettlementProcessor,
    alerts: TelegramAlerter, intent: TradeIntent, clock: Callable[[], datetime],
) -> AttributedFills:
    """One-shot recovery for an order the live poll window already gave up
    on: attribute whatever validated fill activities exist right now via
    attribute_order_fills (safe and idempotent regardless of intent status),
    and if the intent isn't already terminal, check the order's CURRENT
    broker status and finalize it to FILLED/PARTIALLY_FILLED/REJECTED --
    mirrors _poll_and_settle's terminal-status branch as a single pass, no
    polling loop or timeout. Returns the attributed quantity/price."""
    if intent.broker_order_id is None:
        return AttributedFills(Decimal("0"), None)
    attributed = await attribute_order_fills(repositories, broker, alerts, intent, clock)
    if intent.status in TERMINAL_STATUSES:
        return attributed  # already finalized -- the new Fill/SettlementEvent will be
        # picked up by the normal `tradepulse settle` cadence
    try:
        order = await broker.get_order(intent.broker_order_id)
    except AlpacaError:
        return attributed

    cumulative_filled = order.filled_qty
    if attributed.quantity > cumulative_filled:
        await alerts.send(
            "critical",
            f"BROKER_FILL_INTEGRITY_MISMATCH: attributed fill quantity ({attributed.quantity}) exceeds order.filled_qty "
            f"({cumulative_filled}) for {intent.asset.symbol} order {intent.broker_order_id} during late-fill recovery.",
            {"trade_intent_id": intent.trade_intent_id, "broker_order_id": intent.broker_order_id},
        )
        return attributed

    if order.status in TERMINAL_ORDER_STATUSES and attributed.quantity >= cumulative_filled:
        terminal_status = terminal_status_for_order(order.status, attributed.quantity, intent.requested_quantity)
        if terminal_status is None:
            if order.status == "filled":
                # A broker-side contradiction (status=filled but attributed
                # quantity doesn't cover what was requested) -- never
                # finalize a TradeIntent as FILLED without full evidence.
                await alerts.send(
                    "critical",
                    f"BROKER_ORDER_INTEGRITY_MISMATCH: order {intent.broker_order_id} for {intent.asset.symbol} reports "
                    f"status=filled but attributed quantity ({attributed.quantity}) is less than requested "
                    f"({intent.requested_quantity}) during late-fill recovery.",
                    {"trade_intent_id": intent.trade_intent_id, "broker_order_id": intent.broker_order_id,
                     "attributed_qty": str(attributed.quantity), "requested_quantity": str(intent.requested_quantity)},
                )
            # else: done_for_day with zero fills today -- inconclusive, not
            # an error; Alpaca may still send updates the next trading day.
            return attributed
        updated = replace(
            intent, status=terminal_status, filled_quantity=attributed.quantity,
            filled_avg_price=attributed.avg_price or intent.filled_avg_price,
        )
        await repositories.trade_intents.update(intent.trade_intent_id, updated, status=updated.status.value)
        await settlement.process_pending()
    elif attributed.quantity != intent.filled_quantity:
        updated = replace(
            intent, filled_quantity=attributed.quantity, filled_avg_price=attributed.avg_price or intent.filled_avg_price,
            status=TradeIntentStatus.PARTIALLY_FILLED if attributed.quantity > 0 else intent.status,
        )
        await repositories.trade_intents.update(intent.trade_intent_id, updated, status=updated.status.value)

    return attributed


__all__ = [
    "TERMINAL_FAILURE_ORDER_STATUSES",
    "TERMINAL_ORDER_STATUSES",
    "TERMINAL_STATUSES",
    "AttributedFills",
    "attribute_order_fills",
    "resolve_order_from_broker",
    "terminal_status_for_order",
]
