"""Rev.140 task 3: the pure two-bound sellable inventory (spec §3.1)."""
from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.broker.port import OrderState
from tradepulse.execution.sellable import (
    PendingSell,
    SellableDecision,
    SellableEvidenceError,
    SellableInputs,
    build_pending_set,
    compute_sellable,
    size_sell,
)
from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Side, TradeIntent, TradeIntentStatus

AT = datetime(2026, 10, 28, 15, tzinfo=UTC)
AAPL = AssetIdentity(asset_class=AssetClass.EQUITY, symbol="AAPL", native_asset_id="alpaca:AAPL")
KEY = "equity:default:alpaca:AAPL"


def _order(order_id: str, qty: object = "10", filled: str = "0", status: str = "new", coid: str = "",
           symbol: str = "AAPL", side: Side = Side.SELL) -> OrderState:
    return OrderState(broker_order_id=order_id, status=status, symbol=symbol, side=side, filled_qty=Decimal(filled),
                      filled_avg_price=None, submitted_at=AT, request_id=None,
                      raw={"qty": qty, "client_order_id": coid or f"x-{order_id}"})


def _intent(intent_id: str, qty: str = "10", status: TradeIntentStatus = TradeIntentStatus.SUBMITTED,
            filled: str = "0", order_id: str | None = None, side: Side = Side.SELL) -> TradeIntent:
    return TradeIntent(trade_intent_id=intent_id, idempotency_key=f"ik-{intent_id}", correlation_id="c",
                       asset=AAPL, side=side, execution_mode=ExecutionMode.PAPER, strategy="monitor_exit",
                       created_at=AT, requested_quantity=Decimal(qty), status=status, filled_quantity=Decimal(filled),
                       broker_order_id=order_id, client_order_id=intent_id)


def _pending(*members: PendingSell) -> tuple[PendingSell, ...]:
    return members


def test_partial_fill_delayed_settlement_neither_released_nor_double_counted() -> None:
    # L=10, P=6 (4 filled, reflected), intent a=10 s=0, order q=10 filled 4
    d = compute_sellable(SellableInputs(Decimal(6), Decimal(10), (PendingSell("o1", Decimal(10), "i1", Decimal(10), Decimal(0)),)))
    assert (d.local_bound, d.broker_bound, d.sellable) == (Decimal(0), Decimal(-4), Decimal(0))


def test_terminal_order_absent_from_both_reads_stays_member() -> None:
    intent = _intent("i1", status=TradeIntentStatus.FILLED, filled="10", order_id="o1")
    pending = build_pending_set(KEY, [], [], [intent], {"i1": Decimal(0)})
    assert pending == (PendingSell("o1", Decimal(10), "i1", Decimal(10), Decimal(0)),)
    assert compute_sellable(SellableInputs(Decimal(0), Decimal(10), pending)).local_bound == Decimal(0)


def test_terminal_and_fully_settled_intent_leaves_the_set() -> None:
    intent = _intent("i1", status=TradeIntentStatus.FILLED, filled="10", order_id="o1")
    assert build_pending_set(KEY, [], [], [intent], {"i1": Decimal(10)}) == ()


def test_tradepulse_order_counted_once_across_reads_and_intent() -> None:
    order = _order("o1", filled="4", status="partially_filled", coid="i1")
    pending = build_pending_set(KEY, [order], [order], [_intent("i1", filled="4", order_id="o1")], {"i1": Decimal(0)})
    assert pending == (PendingSell("o1", Decimal(10), "i1", Decimal(10), Decimal(0)),)


def test_order_matched_by_client_order_id_before_the_intent_records_it() -> None:
    order = _order("o1", coid="i1")
    pending = build_pending_set(KEY, [order], [order], [_intent("i1")], {})
    assert pending == (PendingSell("o1", Decimal(10), "i1", Decimal(10), Decimal(0)),)


def test_submission_unknown_counts_full() -> None:
    intent = _intent("i1", qty="7", status=TradeIntentStatus.SUBMISSION_UNKNOWN)
    pending = build_pending_set(KEY, [], [], [intent], {})
    assert pending == (PendingSell("intent:i1", Decimal(7), "i1", Decimal(7), Decimal(0)),)
    assert compute_sellable(SellableInputs(Decimal(10), Decimal(10), pending)).local_bound == Decimal(3)


def test_out_of_band_open_order_full_q_in_both_bounds() -> None:
    order = _order("ext", qty="3", filled="1", status="partially_filled")
    pending = build_pending_set(KEY, [order], [order], [], {})
    assert pending == (PendingSell("ext", Decimal(3), None, None, Decimal(0)),)
    d = compute_sellable(SellableInputs(Decimal(10), Decimal(10), pending))
    assert (d.local_bound, d.broker_bound, d.sellable) == (Decimal(7), Decimal(7), Decimal(7))


def test_settled_sell_lagging_in_positions_beside_an_external_order() -> None:
    """Amendment A1, the counterexample the seeded lemma test found: a settled
    TradePulse sell of 3 (2 not yet in positions) and an external open sell of
    6, with 1 more TradePulse share pending. H=33, W=26."""
    pending = (PendingSell("o1", Decimal(1), "i1", Decimal(1), Decimal(0)),
               PendingSell("x2", Decimal(6), None, None, Decimal(0)))
    d = compute_sellable(SellableInputs(Decimal(35), Decimal(33), pending))
    assert (d.local_bound, d.broker_bound, d.sellable) == (Decimal(26), Decimal(28), Decimal(26))


def test_other_assets_and_buys_are_not_members() -> None:
    orders = [_order("msft", symbol="MSFT"), _order("buy", side=Side.BUY)]
    intents = [_intent("ibuy", side=Side.BUY)]
    assert build_pending_set(KEY, orders, orders, intents, {}) == ()


@pytest.mark.parametrize("second", [
    _order("o1", filled="3", status="partially_filled"),  # filled_qty moved
    _order("o1", filled="2", status="filled"),  # status moved
])
def test_reads_disagree_raises_unstable(second: OrderState) -> None:
    first = _order("o1", filled="2", status="partially_filled")
    with pytest.raises(SellableEvidenceError, match="SELLABLE_SNAPSHOT_UNSTABLE"):
        build_pending_set(KEY, [first], [second], [], {})


def test_an_order_in_only_one_read_is_unstable() -> None:
    with pytest.raises(SellableEvidenceError, match="SELLABLE_SNAPSHOT_UNSTABLE"):
        build_pending_set(KEY, [_order("o1")], [], [], {})


def test_filled_above_qty_invalid() -> None:
    order = _order("o1", qty="10", filled="11", status="filled")
    with pytest.raises(SellableEvidenceError, match="INVENTORY_EVIDENCE_INVALID"):
        build_pending_set(KEY, [order], [order], [], {})


@pytest.mark.parametrize("qty", [None, "", "abc", "-1", "0", 10.0])
def test_notional_open_order_refuses(qty: object) -> None:
    order = _order("o1", qty=qty)
    with pytest.raises(SellableEvidenceError, match="INVENTORY_EVIDENCE_INVALID"):
        build_pending_set(KEY, [order], [order], [], {})


@pytest.mark.parametrize(("broker", "local"), [(Decimal(-1), Decimal(10)), (Decimal(10), Decimal(-1))])
def test_negative_p_or_l_invalid(broker: Decimal, local: Decimal) -> None:
    with pytest.raises(SellableEvidenceError, match="INVENTORY_EVIDENCE_INVALID"):
        compute_sellable(SellableInputs(broker, local, ()))


def test_size_sell_reduces_protective_refuses_other() -> None:
    decision = SellableDecision(Decimal(6), Decimal(6), Decimal(8))
    assert size_sell(decision, Decimal(10), protective=True) == (Decimal(6), "SELLABLE_BELOW_REQUESTED")
    assert size_sell(decision, Decimal(10), protective=False) == (Decimal(0), "INSUFFICIENT_SELLABLE_QUANTITY")
    assert size_sell(decision, Decimal(5), protective=False) == (Decimal(5), None)
    assert size_sell(decision, Decimal(6), protective=True) == (Decimal(6), None)
    nothing = SellableDecision(Decimal(0), Decimal(0), Decimal(-4))
    assert size_sell(nothing, Decimal(10), protective=True) == (Decimal(0), "INSUFFICIENT_SELLABLE_QUANTITY")


def test_lemma_seeded_interleavings() -> None:
    """Spec §3.1 lemma: sellable <= max(0, W), W = H - Σ true unfilled remainder, for every
    world in which every order is visible or TradePulse's. 10,000 seeded worlds:
    partial fills, position lag on any fill, unsettled TradePulse fills,
    external open orders, and buys whose fills positions have not reflected."""
    rng = random.Random(140)
    checked = 0
    while checked < 10_000:
        start = Decimal(rng.randint(0, 40))  # broker == local holdings before these orders
        orders, intents, settled = [], [], {}
        true_fills = lagging = Decimal(0)
        settled_sells = Decimal(0)
        outstanding = Decimal(0)
        for n in range(rng.randint(0, 4)):
            q = Decimal(rng.randint(1, 12))
            f = Decimal(rng.randint(0, int(q)))
            external = rng.random() < 0.3
            terminal = f == q
            if external and terminal:
                continue  # invisible to every read: outside the lemma (detected by reconciliation)
            true_fills += f
            lagging += Decimal(rng.randint(0, int(f)))  # not yet reflected in positions
            if not terminal:
                outstanding += q - f
            if external:
                order = _order(f"x{n}", qty=str(q), filled=str(f), status="partially_filled" if f else "new")
                orders.append(order)
                continue
            s = Decimal(rng.randint(0, int(f)))
            settled_sells += s
            settled[f"i{n}"] = s
            status = TradeIntentStatus.FILLED if terminal else TradeIntentStatus.PARTIALLY_FILLED if f else TradeIntentStatus.ACCEPTED
            intents.append(_intent(f"i{n}", qty=str(q), status=status, filled=str(f), order_id=f"o{n}"))
            if not terminal:
                orders.append(_order(f"o{n}", qty=str(q), filled=str(f), coid=f"i{n}",
                                     status="partially_filled" if f else "new"))
        if true_fills > start:
            continue  # cannot sell more than was held
        held = start - true_fills
        unreflected_buys = Decimal(rng.randint(0, 3))  # a lagging buy only lowers P
        broker = held + lagging
        if broker - unreflected_buys >= 0:
            broker -= unreflected_buys
        local = start - settled_sells
        pending = build_pending_set(KEY, orders, list(orders), intents, settled)
        decision = compute_sellable(SellableInputs(broker, local, pending))
        # sellable is clamped at 0, so when pending sells already exceed holdings (W < 0) the claim is 0.
        assert decision.sellable <= max(Decimal(0), held - outstanding), (start, orders, intents, settled, decision)
        checked += 1


def test_option_orders_are_keyed_as_options_not_equities() -> None:
    """OCC symbols keep the option asset key (and so its multiplier)."""
    option = AssetIdentity(asset_class=AssetClass.OPTION, symbol="IWM261030C00286000",
                           native_asset_id="alpaca:IWM261030C00286000")
    from tradepulse.models.market import asset_identity_key

    key = asset_identity_key(option)
    order = _order("o1", qty="1", symbol="IWM261030C00286000")
    assert build_pending_set(key, [order], [order], [], {}) == (PendingSell("o1", Decimal(1), None, None, Decimal(0)),)
    assert build_pending_set("equity:default:alpaca:IWM261030C00286000", [order], [order], [], {}) == ()
