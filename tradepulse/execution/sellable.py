"""The conservative sellable bound (Rev.140, spec §3.1). Pure: no I/O.

The broker's reads are not atomic, so nothing here tries to rebuild one
consistent broker state. Two bounds, each conservative under a different
kind of lag, are computed and the smaller is authorized:

- the local bound, L - Σ (a - s) over TradePulse's own pending sells - Σ q
  over the external open sell orders, which reads no positions;
- the broker bound, P - Σ q over every pending sell order at its original
  quantity, which catches local inventory the broker does not hold.

The local bound subtracts external orders too (a Rev.140 implementation
finding, spec §3.1 amendment A1). Without that, a TradePulse sell that has
settled and left Ω while positions still lag it, alongside an external
open order, let both bounds exceed what is truly free to sell. L does not
depend on positions, so L - Σ (a - s) - Σ external q is W less the
unsettled buys, at most W, whatever the lag.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tradepulse.broker.port import OrderState
from tradepulse.broker.symbols import infer_alpaca_asset_class
from tradepulse.models import Side, TradeIntent, TradeIntentStatus
from tradepulse.models.market import asset_identity_key, asset_key_from_broker_symbol

_TERMINAL = frozenset({TradeIntentStatus.FILLED, TradeIntentStatus.CANCELED, TradeIntentStatus.EXPIRED,
                       TradeIntentStatus.REJECTED, TradeIntentStatus.FAILED})


class SellableEvidenceError(Exception):
    """The evidence cannot support any sellable quantity; the sell waits."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PendingSell:
    """One member of the order set Ω. ``key`` is the broker order ID, or
    ``intent:<trade_intent_id>`` before the broker has one."""

    key: str
    original_quantity: Decimal
    trade_intent_id: str | None
    approved_quantity: Decimal | None
    settled_quantity: Decimal


@dataclass(frozen=True, slots=True)
class SellableInputs:
    broker_quantity: Decimal
    local_long_quantity: Decimal
    pending: tuple[PendingSell, ...]


@dataclass(frozen=True, slots=True)
class SellableDecision:
    sellable: Decimal
    local_bound: Decimal
    broker_bound: Decimal


def _order_quantity(order: OrderState) -> Decimal:
    """The order's original quantity. A notional order (qty null) or an
    unparsable one is not evidence of a quantity, never zero."""
    raw = order.raw.get("qty")
    if isinstance(raw, (bool, float)) or raw is None:
        raise SellableEvidenceError("INVENTORY_EVIDENCE_INVALID")
    try:
        quantity = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise SellableEvidenceError("INVENTORY_EVIDENCE_INVALID") from exc
    if not quantity.is_finite() or quantity <= 0 or order.filled_qty > quantity or order.filled_qty < 0:
        raise SellableEvidenceError("INVENTORY_EVIDENCE_INVALID")
    return quantity


def _sells_of(asset_key: str, orders: Sequence[OrderState]) -> dict[str, OrderState]:
    return {order.broker_order_id: order for order in orders if order.side == Side.SELL
            and asset_key_from_broker_symbol(infer_alpaca_asset_class(order.symbol), order.symbol) == asset_key}


def build_pending_set(asset_key: str, open_orders_1: Sequence[OrderState], open_orders_2: Sequence[OrderState],
                      intents: Sequence[TradeIntent], settled: Mapping[str, Decimal]) -> tuple[PendingSell, ...]:
    """Ω for one asset: every sell order in either open-order read, plus every
    TradePulse sell intent not yet terminal **and** fully settled (S1). An
    intent's broker order is the same member, matched by broker order ID or
    client order ID, never a second one."""
    first, second = _sells_of(asset_key, open_orders_1), _sells_of(asset_key, open_orders_2)
    if first.keys() != second.keys() or any(
            (first[i].status, first[i].filled_qty) != (second[i].status, second[i].filled_qty) for i in first):
        raise SellableEvidenceError("SELLABLE_SNAPSHOT_UNSTABLE")
    quantities = {order_id: _order_quantity(order) for order_id, order in first.items()}
    by_client_id = {str(order.raw.get("client_order_id") or ""): order_id for order_id, order in first.items()}

    members: list[PendingSell] = []
    claimed: set[str] = set()
    for intent in intents:
        if intent.side != Side.SELL or asset_identity_key(intent.asset) != asset_key:
            continue
        settled_quantity = settled.get(intent.trade_intent_id, Decimal(0))
        if intent.status in _TERMINAL and settled_quantity >= intent.filled_quantity:
            continue
        if intent.requested_quantity is None or intent.requested_quantity <= 0:
            raise SellableEvidenceError("INVENTORY_EVIDENCE_INVALID")
        order_id = intent.broker_order_id or by_client_id.get(intent.client_order_id or "")
        claimed.add(order_id or "")
        members.append(PendingSell(
            key=order_id or f"intent:{intent.trade_intent_id}",
            original_quantity=quantities.get(order_id or "", intent.requested_quantity),
            trade_intent_id=intent.trade_intent_id, approved_quantity=intent.requested_quantity,
            settled_quantity=settled_quantity))
    for order_id in sorted(first.keys() - claimed):
        members.append(PendingSell(order_id, quantities[order_id], None, None, Decimal(0)))
    return tuple(members)


def compute_sellable(inputs: SellableInputs) -> SellableDecision:
    if inputs.broker_quantity < 0 or inputs.local_long_quantity < 0:
        raise SellableEvidenceError("INVENTORY_EVIDENCE_INVALID")
    local = inputs.local_long_quantity - sum(
        (member.approved_quantity - member.settled_quantity
         if member.trade_intent_id is not None and member.approved_quantity is not None
         else member.original_quantity for member in inputs.pending), Decimal(0))
    broker = inputs.broker_quantity - sum((member.original_quantity for member in inputs.pending), Decimal(0))
    return SellableDecision(max(Decimal(0), min(local, broker)), local, broker)


def size_sell(decision: SellableDecision, requested: Decimal, *, protective: bool) -> tuple[Decimal, str | None]:
    """A protective exit is reduced to sellable rather than refused, so the
    bound's conservatism never costs protection; any other sell above
    sellable is refused."""
    if decision.sellable <= 0:
        return Decimal(0), "INSUFFICIENT_SELLABLE_QUANTITY"
    if requested <= decision.sellable:
        return requested, None
    if protective:
        return decision.sellable, "SELLABLE_BELOW_REQUESTED"
    return Decimal(0), "INSUFFICIENT_SELLABLE_QUANTITY"
