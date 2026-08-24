from decimal import Decimal

import pytest

from tradepulse.execution.fill_attribution import terminal_status_for_order
from tradepulse.models import TradeIntentStatus


@pytest.mark.parametrize(
    ("order_status", "attributed_qty", "requested_quantity", "expected"),
    [
        # done_for_day is not a permanent disposition -- Alpaca may still
        # send further updates the next trading day, so a genuine partial
        # maps to the same non-terminal-in-this-system PARTIALLY_FILLED a
        # live partial fill would get, and a zero-fill day is inconclusive
        # (None) rather than any kind of false-cancellation.
        ("done_for_day", Decimal("4"), Decimal("10"), TradeIntentStatus.PARTIALLY_FILLED),
        ("done_for_day", Decimal("10"), Decimal("10"), TradeIntentStatus.FILLED),
        ("done_for_day", Decimal("0"), Decimal("10"), None),
        # Broker failure statuses map to their OWN distinct TradeIntentStatus
        # when nothing was attributed, not a single generic REJECTED.
        ("canceled", Decimal("4"), Decimal("10"), TradeIntentStatus.PARTIALLY_FILLED),
        ("canceled", Decimal("0"), Decimal("10"), TradeIntentStatus.CANCELED),
        ("expired", Decimal("0"), Decimal("10"), TradeIntentStatus.EXPIRED),
        ("rejected", Decimal("0"), Decimal("10"), TradeIntentStatus.REJECTED),
        ("replaced", Decimal("0"), Decimal("10"), TradeIntentStatus.CANCELED),
        # `filled` always means the order's own full submitted quantity was
        # executed -- anything less is a broker-side contradiction, not
        # evidence of a real partial fill, and must never be finalized.
        ("filled", Decimal("10"), Decimal("10"), TradeIntentStatus.FILLED),
        ("filled", Decimal("0"), Decimal("10"), None),
        ("filled", Decimal("4"), Decimal("10"), None),
    ],
)
def test_terminal_status_for_order_is_quantity_aware(order_status, attributed_qty, requested_quantity, expected) -> None:
    assert terminal_status_for_order(order_status, attributed_qty, requested_quantity) == expected


def test_terminal_status_for_order_treats_missing_requested_quantity_as_unresolvable_filled() -> None:
    """No requested_quantity to compare against -- can't confirm full
    attribution, so `filled` must not be forced through; the caller is
    expected to leave the intent non-terminal rather than guess."""
    assert terminal_status_for_order("filled", Decimal("10"), None) is None
