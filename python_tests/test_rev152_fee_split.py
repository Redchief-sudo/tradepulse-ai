"""Rev.152 F151-01 (Rev.151 audit, 2026-11-06): the order-level fee split
divides by the sum of its weights, so an empty, zero-total or non-positive
weight set must be refused with a named integrity error, never a bare
decimal.InvalidOperation or a silently wrong allocation. The split must
conserve the amount exactly for every valid input.
"""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradepulse.adoption.accounting import FeeSplitError, _split


@pytest.mark.parametrize(("amount", "weights"), [
    (Decimal("1.00"), {"a": Decimal(0), "b": Decimal(0)}),
    (Decimal("0.00"), {"a": Decimal(0)}),
    (Decimal("1.00"), {}),
    (Decimal("1.00"), {"a": Decimal(1), "b": Decimal(0)}),
    (Decimal("1.00"), {"a": Decimal(2), "b": Decimal(-1)}),
])
def test_an_invalid_weight_set_is_refused_by_name(amount, weights) -> None:
    with pytest.raises(FeeSplitError, match="FEE_SPLIT_WEIGHTS_INVALID"):
        _split(amount, weights)


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_a_non_finite_amount_is_refused_by_name(amount) -> None:
    with pytest.raises(FeeSplitError, match="FEE_SPLIT_AMOUNT_INVALID"):
        _split(amount, {"a": Decimal(1)})


def test_the_audit_cases_that_were_correct_stay_correct() -> None:
    assert _split(Decimal("0.001"), {"a": Decimal(1), "b": Decimal(1)}) == {"a": Decimal("0.001"), "b": Decimal(0)}
    assert _split(Decimal(0), {"a": Decimal(1), "b": Decimal(3)}) == {"a": Decimal(0), "b": Decimal(0)}
    assert _split(Decimal(-2), {"a": Decimal(4), "b": Decimal(6)}) == {"a": Decimal("-0.80"), "b": Decimal("-1.20")}


_amounts = st.decimals(min_value=Decimal(-1000000), max_value=Decimal(1000000), allow_nan=False,
                       allow_infinity=False, places=6)
_weights = st.dictionaries(st.text(alphabet="abcdefgh", min_size=1, max_size=3),
                           st.decimals(min_value=Decimal("0.000001"), max_value=Decimal(100000), allow_nan=False,
                                       allow_infinity=False, places=6),
                           min_size=1, max_size=8)


@given(_amounts, _weights)
def test_a_valid_split_conserves_the_amount_exactly(amount, weights) -> None:
    parts = _split(amount, weights)
    assert set(parts) == set(weights)
    assert sum(parts.values(), Decimal(0)) == amount
    assert all(part == 0 or (part > 0) == (amount > 0) for part in parts.values())


def test_a_split_that_loses_a_cent_is_refused(monkeypatch) -> None:
    """F151-03: the allocation proves its own conservation. A split that does
    not sum to its receipt (simulated here) is refused, never posted."""
    from test_rev142_accounting import _adopt, _db, _fee, _sell_partial

    from tradepulse.adoption import accounting

    connection = _db()
    lot_id = _adopt(connection, qty="10", basis="100", mark="110")
    _sell_partial(connection, lot_id, "4", "115", "0", "sell-a", "order-x")
    _sell_partial(connection, lot_id, "6", "115", "0", "sell-b", "order-x")
    _fee(connection, "f1", {"order_id": "order-x"}, "-2")
    fills = [accounting.hydrate("fills", accounting.decode_payload(row[0]))
             for row in connection.execute("SELECT payload FROM fills ORDER BY record_id")]
    assert sum(accounting.closing_fee_allocation(connection, fills).values(), Decimal(0)) == 2

    real = accounting._split
    monkeypatch.setattr(accounting, "_split", lambda amount, weights: {
        key: value - Decimal("0.01") if key == "sell-a" else value for key, value in real(amount, weights).items()})
    with pytest.raises(FeeSplitError, match="FEE_SPLIT_NOT_CONSERVED: generation_fee:f1"):
        accounting.closing_fee_allocation(connection, fills)


def test_each_receipt_is_applied_once_across_repeated_allocation() -> None:
    """F151-03: replaying the allocation (a restart, a retry) gives the same
    figures; a receipt for another order is never applied here."""
    from test_rev142_accounting import _adopt, _db, _fee, _sell_partial

    from tradepulse.adoption import accounting

    connection = _db()
    lot_id = _adopt(connection, qty="10", basis="100", mark="110")
    _sell_partial(connection, lot_id, "5", "115", "0.01", "sell-a", "order-x")
    _sell_partial(connection, lot_id, "5", "115", "0.01", "sell-b", "order-x")
    _fee(connection, "mine", {"order_id": "order-x"}, "-2")
    _fee(connection, "other", {"order_id": "someone-else"}, "-3")
    fills = [accounting.hydrate("fills", accounting.decode_payload(row[0]))
             for row in connection.execute("SELECT payload FROM fills ORDER BY record_id")]
    first = accounting.closing_fee_allocation(connection, fills)
    assert all(accounting.closing_fee_allocation(connection, fills) == first for _ in range(3))
    assert sum(first.values(), Decimal(0)) == Decimal("2.02")
