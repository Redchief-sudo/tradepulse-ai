from decimal import Decimal

from tradepulse.valuation import broker_settled_cash


def _fill(identifier, amount):
    return {"entry_id": "fill:cash:" + identifier, "amount": amount}


def test_fractional_fill_cash_settles_per_fill_to_the_cent_like_the_broker():
    # Real soak receipts: an 8.124-share AAPL order filled in four executions.
    # Alpaca debited 2729.12 (per-fill cents), not the exact 2729.11656.
    entries = [_fill("a", "-2015.58"), _fill("b", "-335.94"), _fill("c", "-335.94"), _fill("d", "-41.65656")]
    opening_cash = Decimal("101364.15")
    assert opening_cash + sum(broker_settled_cash(e) for e in entries) == Decimal("98635.03")


def test_per_fill_rounding_is_symmetric_and_non_fill_entries_are_unchanged():
    assert broker_settled_cash(_fill("sell", "102.72003")) == Decimal("102.72")
    assert broker_settled_cash(_fill("buy", "-0.005")) == Decimal("-0.01")
    assert broker_settled_cash({"entry_id": "broker:fee:x", "amount": "-0.0042"}) == Decimal("-0.0042")
