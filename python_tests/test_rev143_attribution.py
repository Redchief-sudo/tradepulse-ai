"""Rev.143 task 3: the attribution decision (spec §3.4-3.5; T6-T12, T16, T17, T19, T27-T34)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tradepulse.reconciliation.dividend_attribution import (
    Announcement,
    Inherited,
    Member,
    StrategyLot,
    Unresolved,
    decide,
    eligible_strategy,
    inherited_quantity,
    mirror,
    split,
    trade_date,
)

GROSS = "gross_and_separate_withholding"
NET = "net_with_withholding_record"
EX = date(2026, 11, 10)
CREDIT = date(2026, 11, 13)


def ann(identifier: str = "a1", *, cash: str = "0.26", ex: str = "2026-11-10", payable: str = "2026-11-13", symbol: str = "AAPL",
        **fields: Any) -> Announcement:
    raw = {"id": identifier, "ca_type": "dividend", "ca_sub_type": "cash", "initiating_symbol": symbol, "target_symbol": symbol,
           "ex_date": ex, "record_date": ex, "payable_date": payable, "cash": cash, **fields}
    return Announcement(identifier, "sha-" + identifier, raw)


def income(amount: str = "2.60", qty: str = "10", rate: str = "0.26", identifier: str = "d1") -> Member:
    return Member(identifier, "income", Decimal(amount), Decimal(qty), Decimal(rate), {})


def withholding(amount: str, identifier: str = "w1", **raw: Any) -> Member:
    return Member(identifier, "withholding", Decimal(amount), Decimal(10), Decimal("0.26"), raw)


def lot(lot_id: str, qty: str, opened: datetime, *closures: tuple[str, datetime]) -> StrategyLot:
    return StrategyLot(lot_id, Decimal(qty), opened, tuple((Decimal(q), at) for q, at in closures))


BEFORE = datetime(2026, 11, 9, 15, tzinfo=UTC)  # 10:00 NY the day before the ex-date
NO_RUN: Callable[[date], Inherited | None] = lambda ex: None
ESTABLISHED_ZERO: Callable[[date], Inherited | None] = lambda ex: Inherited(True, Decimal(0))
UNESTABLISHED: Callable[[date], Inherited | None] = lambda ex: Inherited(False, None, "x")


def run(group: list[Member], announcements: list[Announcement], lots: list[StrategyLot], inherited: Any = ESTABLISHED_ZERO,
        representation: str = GROSS, quarantined: bool = False, consumed: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    return decide(symbol="AAPL", credit=CREDIT, group=group, announcements=announcements, consumed=consumed, lots=lots,
                  inherited_for=inherited, quarantined=quarantined, representation=representation)


def test_one_match_verifies_and_a_late_credit_still_matches() -> None:
    """T6: one announcement -> verified; payable 11-13, credited later still matches."""
    decision = run([income()], [ann(payable="2026-11-12")], [lot("L1", "10", BEFORE)])
    assert (decision.attribution_status, decision.entitlement_status) == ("verified", "established")
    assert [(r["bucket"], r["holder_id"], r["amount"], r["announcement_sha256"]) for r in decision.rows] == [
        ("strategy", "L1", "2.60", "sha-a1")]


def test_matching_zero_two_and_disambiguation() -> None:
    """T6: none -> pending; two same-rate announcements told apart by quantity
    at each ex-date; indistinguishable -> pending; consumed ones are skipped."""
    assert run([income()], [], [lot("L1", "10", BEFORE)]).reason == "DIVIDEND_ANNOUNCEMENT_NOT_FOUND"
    assert run([income()], [ann(cash="0.25")], [lot("L1", "10", BEFORE)]).reason == "DIVIDEND_RATE_MISMATCH"
    august = ann("a0", ex="2026-08-10", payable="2026-08-13")
    bought_september = lot("L1", "10", datetime(2026, 9, 1, 15, tzinfo=UTC))
    decision = run([income()], [august, ann()], [bought_september])
    assert decision.announcement is not None and decision.announcement.announcement_id == "a1"  # 0 shares at the August ex-date
    held_both = lot("L1", "10", datetime(2026, 7, 1, 15, tzinfo=UTC))
    assert run([income()], [august, ann()], [held_both]).reason == "DIVIDEND_ANNOUNCEMENT_AMBIGUOUS"
    assert run([income()], [august, ann()], [held_both], consumed=frozenset({"a0"})).attribution_status == "verified"


def test_special_dividend() -> None:
    """T7: ex-date after the payable date."""
    decision = run([income()], [ann(ex="2026-11-14", payable="2026-11-13")], [lot("L1", "10", datetime(2026, 11, 13, 15, tzinfo=UTC))])
    assert (decision.attribution_status, decision.reason) == ("unresolved", "DIVIDEND_SPECIAL_EX_DATE")


@pytest.mark.parametrize(("opened", "entitled"), [
    (datetime(2026, 11, 9, 23, 30, tzinfo=UTC), True),  # 18:30 NY the evening before, after hours: entitled
    (datetime(2026, 11, 10, 13, tzinfo=UTC), False),  # 08:00 NY pre-market on the ex-date: not entitled
    (datetime(2026, 11, 6, 15, tzinfo=UTC), True),
])
def test_entitlement_by_trade_date(opened: datetime, entitled: bool) -> None:
    """T8."""
    assert ("L1" in eligible_strategy([lot("L1", "10", opened)], EX)) == entitled


def test_closures_and_overnight_ambiguity() -> None:
    """T8: sold on or after the ex-date keeps it; sold before loses it; an
    overnight fill (20:00-24:00 ET the evening before) is ambiguous."""
    sold_on_ex = lot("L1", "10", BEFORE, ("4", datetime(2026, 11, 10, 15, tzinfo=UTC)))
    sold_before = lot("L2", "10", BEFORE, ("4", datetime(2026, 11, 9, 16, tzinfo=UTC)))
    assert eligible_strategy([sold_on_ex, sold_before], EX) == {"L1": Decimal(10), "L2": Decimal(6)}
    with pytest.raises(Unresolved, match="DIVIDEND_TRADE_DATE_AMBIGUOUS"):
        trade_date(datetime(2026, 11, 10, 2, tzinfo=UTC), EX)  # 21:00 NY on 11-09
    decision = run([income()], [ann()], [lot("L1", "10", datetime(2026, 11, 10, 2, tzinfo=UTC))])
    assert decision.reason == "DIVIDEND_TRADE_DATE_AMBIGUOUS"
    assert trade_date(datetime(2026, 11, 6, 2, tzinfo=UTC), EX) == date(2026, 11, 6)  # 21:00 NY 11-05 -> the next trading day


def test_quantity_and_amount_checks_have_no_tolerance() -> None:
    """T9, T10."""
    assert run([income()], [ann()], [lot("L1", "9", BEFORE)]).reason == "DIVIDEND_QUANTITY_MISMATCH"
    assert run([income(amount="2.61")], [ann()], [lot("L1", "10", BEFORE)]).reason == "DIVIDEND_AMOUNT_MISMATCH"
    assert run([income(amount="2.59")], [ann()], [lot("L1", "10", BEFORE)]).reason == "DIVIDEND_AMOUNT_MISMATCH"


def test_withholding_split_in_the_income_s_proportions() -> None:
    """T11, T29 (gross representation): DIV +2.60 and DIVNRA -0.39 over 6:4 lots."""
    lots = [lot("L1", "6", BEFORE), lot("L2", "4", BEFORE)]
    decision = run([income(), withholding("-0.39")], [ann()], lots)
    rows = {(r["activity_id"], r["holder_id"]): Decimal(r["amount"]) for r in decision.rows}
    assert rows == {("d1", "L1"): Decimal("1.56"), ("d1", "L2"): Decimal("1.04"), ("w1", "L1"): Decimal("-0.23"), ("w1", "L2"): Decimal("-0.16")}
    assert sum(v for (a, _), v in rows.items() if a == "w1") == Decimal("-0.39")
    assert sum(rows.values()) == Decimal("2.21")  # the group's posted cash: two entries, +2.60 and -0.39
    assert run([withholding("-0.39")], [ann()], lots).reason == "DIVIDEND_WITHHOLDING_UNPAIRED"


def test_largest_remainder_split() -> None:
    """T12: three equal lots of a cent amount sum exactly; ties by lot ID."""
    parts = split(Decimal("1.00"), {"lot-c": Decimal(1), "lot-a": Decimal(1), "lot-b": Decimal(1)})
    assert parts == {"lot-a": Decimal("0.34"), "lot-b": Decimal("0.33"), "lot-c": Decimal("0.33")}
    negative = split(Decimal("-1.00"), {"lot-c": Decimal(1), "lot-a": Decimal(1), "lot-b": Decimal(1)})
    assert sum(negative.values()) == Decimal("-1.00") and negative["lot-a"] == Decimal("-0.34")


def test_outside_a_run() -> None:
    """T16: no checkpoint; S = qty verifies, S = 0 is excluded_unallocated, otherwise unexplained."""
    assert run([income()], [ann()], [lot("L1", "10", BEFORE)], inherited=NO_RUN).attribution_status == "verified"
    assert run([income()], [ann()], [], inherited=NO_RUN).attribution_status == "excluded_unallocated"
    partial = run([income()], [ann()], [lot("L1", "4", BEFORE)], inherited=NO_RUN)
    assert (partial.attribution_status, partial.reason) == ("unresolved", "DIVIDEND_ELIGIBLE_QUANTITY_UNEXPLAINED")


def test_quarantined_instrument() -> None:
    """T19 (decision): never attributed while quarantine is open (DV-I7)."""
    decision = run([income()], [ann()], [lot("L1", "10", BEFORE)], quarantined=True)
    assert (decision.attribution_status, decision.reason) == ("unresolved", "DIVIDEND_ON_QUARANTINED_INSTRUMENT")


def test_inherited_without_evidence() -> None:
    """T27: no E1 -> no quantity is ever inferred; S = 0 is excluded_unallocated,
    never verified; S > 0 is DIVIDEND_INHERITED_QUANTITY_UNESTABLISHED."""
    excluded = run([income()], [ann()], [], inherited=UNESTABLISHED)
    assert (excluded.attribution_status, excluded.entitlement_status) == ("excluded_unallocated", "not_established")
    [row] = excluded.rows
    assert (row["bucket"], row["eligible_qty"], row["quantity_evidence"], row["amount"]) == (
        "excluded_unallocated", None, "not_established", "2.60")
    mixed = run([income()], [ann()], [lot("L1", "4", BEFORE)], inherited=UNESTABLISHED)
    assert (mixed.attribution_status, mixed.reason) == ("unresolved", "DIVIDEND_INHERITED_QUANTITY_UNESTABLISHED")


def test_e1_established_and_not() -> None:
    """T28: checkpoint before the ex-date and a clean population -> I; a
    transfer or an unresolved membership in the interval -> not established."""
    checkpoint = {"opened_at": "2026-11-02T14:00:00+00:00",
                  "positions": [{"asset_class": "equity", "symbol": "MSFT", "qty": "13.252"}]}
    population = {"activities": [], "classifications": {}}
    observed = datetime(2026, 11, 13, 15, tzinfo=UTC)
    key = "equity:default:alpaca:MSFT"
    established = inherited_quantity(checkpoint, population, observed, frozenset(), key, EX)
    assert established == Inherited(True, Decimal("13.252"))
    for kind in ("ACATC", "JNLS", "SSP"):
        dirty = {"activities": [{"id": "t1", "activity_type": kind, "symbol": "MSFT", "date": "2026-11-05"}],
                 "classifications": {"t1": "in_generation"}}
        assert not inherited_quantity(checkpoint, dirty, observed, frozenset(), key, EX).established  # type: ignore[union-attr]
    unresolved = {"activities": [], "classifications": {"x": "unresolved_generation_membership"}}
    assert not inherited_quantity(checkpoint, unresolved, observed, frozenset(), key, EX).established  # type: ignore[union-attr]
    late = {**checkpoint, "opened_at": "2026-11-11T14:00:00+00:00"}
    assert not inherited_quantity(late, population, observed, frozenset(), key, EX).established  # type: ignore[union-attr]
    assert inherited_quantity(None, population, observed, frozenset(), key, EX) is None
    msft = decide(symbol="MSFT", credit=CREDIT, group=[Member("d", "income", Decimal("12.06"), Decimal("13.252"), Decimal("0.91"), {})],
                  announcements=[ann(cash="0.91", symbol="MSFT")], consumed=frozenset(), lots=[],
                  inherited_for=lambda ex: established, quarantined=False, representation=GROSS)
    assert (msft.attribution_status, [(r["bucket"], r["eligible_qty"], r["quantity_evidence"]) for r in msft.rows]) == (
        "verified", [("opening_unadopted", "13.252", "checkpoint_plus_activity")])


def test_net_representation() -> None:
    """T30: an informational withholding (no cash) uses its recorded withheld
    amount; a cash debit under the net representation is inconsistent."""
    net = run([income(amount="2.21"), withholding("0", withheld_amount="-0.39")], [ann()], [lot("L1", "10", BEFORE)], representation=NET)
    assert net.attribution_status == "verified"
    assert sum(Decimal(r["amount"]) for r in net.rows) == Decimal("2.21")
    inconsistent = run([income(amount="2.21"), withholding("-0.39")], [ann()], [lot("L1", "10", BEFORE)], representation=NET)
    assert (inconsistent.attribution_status, inconsistent.reason) == ("unresolved", "DIVIDEND_REPRESENTATION_INCONSISTENT")


def test_unknown_representation_refuses_every_dividend() -> None:
    """T31: the G0 constant; no rows are ever written."""
    from tradepulse.reconciliation.dividend_attribution import DIVIDEND_REPRESENTATION

    assert DIVIDEND_REPRESENTATION == "unknown"
    decision = decide(symbol="AAPL", credit=CREDIT, group=[income()], announcements=[ann()], consumed=frozenset(),
                      lots=[lot("L1", "10", BEFORE)], inherited_for=ESTABLISHED_ZERO, quarantined=False)
    assert (decision.attribution_status, decision.reason, decision.rows) == ("unresolved", "DIVIDEND_REPRESENTATION_UNKNOWN", ())


def test_matching_without_quantity() -> None:
    """T33: I not established, S = 0: exactly one candidate plus the amount
    check on the receipt's own qty x rate; two candidates are ambiguous."""
    assert run([income()], [ann()], [], inherited=UNESTABLISHED).attribution_status == "excluded_unallocated"
    two = run([income()], [ann(), ann("a2", ex="2026-08-10", payable="2026-08-13")], [], inherited=UNESTABLISHED)
    assert (two.attribution_status, two.reason) == ("unresolved", "DIVIDEND_ANNOUNCEMENT_AMBIGUOUS")
    assert run([income(amount="2.50")], [ann()], [], inherited=UNESTABLISHED).reason == "DIVIDEND_AMOUNT_MISMATCH"


@pytest.mark.parametrize(("inherited", "lots", "status", "entitlement"), [
    (ESTABLISHED_ZERO, [lot("L1", "10", BEFORE)], "verified", "established"),
    (UNESTABLISHED, [], "excluded_unallocated", "not_established"),
    (UNESTABLISHED, [lot("L1", "4", BEFORE)], "unresolved", "not_established"),
])
def test_status_separation(inherited: Any, lots: list[StrategyLot], status: str, entitlement: str) -> None:
    """T34: step 3a's table; excluded_unallocated is never verified."""
    decision = run([income()], [ann()], lots, inherited=inherited)
    assert (decision.attribution_status, decision.entitlement_status) == (status, entitlement)
    assert not (decision.attribution_status == "verified" and any(r["bucket"] == "excluded_unallocated" for r in decision.rows))


def test_reversal_mirrors_a_verified_group() -> None:
    """T17, T32: mirror rows sum to zero per holder and component; a partial
    or income-only reversal of a group does not mirror."""
    lots = [lot("L1", "6", BEFORE), lot("L2", "4", BEFORE)]
    original = [income(), withholding("-0.39")]
    rows = run(original, [ann()], lots).rows
    reversal = [income(amount="-2.60", identifier="r1"), withholding("0.39", identifier="r2")]
    mirrored = mirror(rows, reversal, original)
    assert mirrored is not None
    totals: dict[tuple[str, str], Decimal] = {}
    for row in (*rows, *mirrored):
        totals[(row["holder_id"], row["component"])] = totals.get((row["holder_id"], row["component"]), Decimal(0)) + Decimal(row["amount"])
    assert set(totals.values()) == {Decimal(0)}
    assert mirror(rows, [income(amount="-2.60", identifier="r1")], original) is None  # the income alone
    assert mirror(rows, [income(amount="-1.30", identifier="r1"), withholding("0.39", identifier="r2")], original) is None  # partial
