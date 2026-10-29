"""Rev.140 task 10: signed fee allocation and conserved recognition (spec §3.6, tests 10-11)."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradepulse.models import AssetClass, AssetIdentity
from tradepulse.models.quarantine import QuarantineResolution, ShortExposureLiability
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.quarantine.fees import (
    BrokerEvidenceChanged,
    allocate_fee,
    check_c1,
    conservation,
    recognize_adjustment,
    recognize_at_resolution,
    record_allocations,
    record_recognition,
)

AT = datetime(2026, 10, 28, 15, tzinfo=UTC)
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


def test_eight_two_on_minus_five_cents() -> None:
    """Spec test 10: proceeds 8:2 on a -0.05 fee. ROUND_DOWN toward zero; the
    quarantined record, last, takes the exact remainder."""
    shares = allocate_fee("fee-1", Decimal("-0.05"), [("attribution:a", Decimal(8))], [("liability:l", Decimal(2), "opening")])
    assert [(s.record_id, s.amount) for s in shares] == [("attribution:a", Decimal("-0.04")), ("liability:l", Decimal("-0.01"))]
    assert sum(s.amount for s in shares) == Decimal("-0.05")


def test_two_one_on_minus_one_cent() -> None:
    shares = allocate_fee("fee-2", Decimal("-0.01"), [("attribution:a", Decimal(2))], [("liability:l", Decimal(1), "opening")])
    assert [s.amount for s in shares] == [Decimal("0.00"), Decimal("-0.01")]


def test_a_reversal_allocates_with_its_own_sign() -> None:
    shares = allocate_fee("fee-3", Decimal("0.05"), [("attribution:a", Decimal(8))], [("liability:l", Decimal(2), "opening")])
    assert [s.amount for s in shares] == [Decimal("0.04"), Decimal("0.01")]


def test_quarantined_targets_are_always_last() -> None:
    shares = allocate_fee("fee-4", Decimal("-0.07"), [("attribution:a", Decimal(1)), ("attribution:b", Decimal(1))],
                          [("liability:l", Decimal(1), "opening")])
    assert [s.record_id for s in shares] == ["attribution:a", "attribution:b", "liability:l"]
    assert sum(s.amount for s in shares) == Decimal("-0.07")


def test_recognition_takes_a_share_of_the_current_balance_and_the_rest_at_the_end() -> None:
    first = recognize_at_resolution("liability:l", "opening", open_before=Decimal(3), covered=Decimal(1),
                                    unrecognized=Decimal("-0.10"), event_id="r1", now=AT)
    assert first.amount == Decimal("-0.03")  # floor of -0.0333 toward zero
    final = recognize_at_resolution("liability:l", "opening", open_before=Decimal(2), covered=Decimal(2),
                                    unrecognized=Decimal("-0.07"), event_id="r2", now=AT)
    assert final.amount == Decimal("-0.07")  # q == q0: exactly U


async def _repositories(tmp_path: Path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path / 'fees.db'}")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def test_signed_conservation_sequence(tmp_path: Path) -> None:
    """Spec test 11: C1 holds after every step of a long fee history."""
    repositories = await _repositories(tmp_path)
    liability = ShortExposureLiability(liability_id="liability:l", fill_id="f", broker_order_id="o", trade_intent_id="i",
                                       asset=AAPL, quantity=Decimal(3), sale_price=Decimal(100), multiplier=Decimal(1),
                                       proceeds=Decimal(300), opened_at=AT)
    await repositories.short_exposure_liabilities.create_once(liability.liability_id, liability, unique_value="f")

    async def c1() -> None:
        assert await check_c1(repositories) == []

    async def allocate(fee_id: str, amount: str, pool: str = "opening") -> None:
        await record_allocations(repositories, allocate_fee(fee_id, Decimal(amount), [], [("liability:l", Decimal(3), pool)]))

    async def resolve(activity: str, quantity: str) -> None:
        open_before = Decimal(3) - sum((Decimal(q) for _, q in resolved), Decimal(0))
        _, _, unrecognized = await conservation(repositories, "liability:l", "opening")
        resolution = QuarantineResolution(record_id="liability:l", activity_id=activity, quantity=Decimal(quantity),
                                          price=Decimal(99), realized_pnl=Decimal(0), resolved_at=AT)
        await repositories.quarantine_resolutions.create_once(f"liability:l|{activity}", resolution,
                                                              unique_value=f"liability:l|{activity}")
        resolved.append((activity, quantity))
        await record_recognition(repositories, recognize_at_resolution(
            "liability:l", "opening", open_before=open_before, covered=Decimal(quantity), unrecognized=unrecognized,
            event_id=activity, now=AT))

    resolved: list[tuple[str, str]] = []
    await allocate("charge", "-0.10")
    await c1()
    await allocate("reversal", "0.10")
    await c1()
    await allocate("repost", "-0.11")
    await c1()
    await resolve("cover-1", "1")
    await c1()
    await allocate("correction", "-0.03")  # arrives between partial resolutions: spread over what remains
    await c1()
    await resolve("cover-2", "2")
    await c1()
    assert (await conservation(repositories, "liability:l", "opening"))[2] == 0
    await allocate("late-correction", "-0.02")  # after full resolution: one dated adjustment
    assert await check_c1(repositories) == ["liability:l|opening"]
    _, _, unrecognized = await conservation(repositories, "liability:l", "opening")
    await record_recognition(repositories, recognize_adjustment("liability:l", "opening", unrecognized, "adjust-1", AT))
    await c1()
    await allocate("cover-fee", "-0.04", pool="cover")  # posts the next evening
    _, _, cover_unrecognized = await conservation(repositories, "liability:l", "cover")
    await record_recognition(repositories, recognize_adjustment("liability:l", "cover", cover_unrecognized, "adjust-2", AT))
    await c1()
    a, r, u = await conservation(repositories, "liability:l", "opening")
    assert (a, r, u) == (Decimal("-0.16"), Decimal("-0.16"), Decimal(0))
    await allocate("charge", "-0.10")  # replay of the same evidence: no-op
    with pytest.raises(BrokerEvidenceChanged, match="BROKER_EVIDENCE_CHANGED"):
        await allocate("charge", "-0.12")  # same fee ID, different amount
    await c1()


def test_an_oversell_order_fee_falls_last_on_its_liability() -> None:
    """allocate_cash_fees_with_quarantine: the closure attribution takes its
    rounded-down share and the liability the exact remainder; a fee with no
    quarantined target is allocated exactly as before."""
    from types import SimpleNamespace

    from tradepulse.reconciliation.cash_fees import allocate_cash_fees, allocate_cash_fees_with_quarantine

    attribution = SimpleNamespace(attribution_id="lot-1:fill-1", closing_trade_intent_id="exit", quantity=Decimal(10),
                                  exit_price=Decimal(100))
    other = SimpleNamespace(attribution_id="lot-2:fill-2", closing_trade_intent_id="plain", quantity=Decimal(5),
                            exit_price=Decimal(100))
    proof = {"activities": [{"id": "fee-oversell", "net_amount": "-0.07"}, {"id": "fee-plain", "net_amount": "-0.03"}],
             "cash_fee_populations": {"fee-oversell": {"method": "authoritative_order_link", "trade_intent_ids": ["exit"]},
                                      "fee-plain": {"method": "authoritative_order_link", "trade_intent_ids": ["plain"]}}}
    plans, shares = allocate_cash_fees_with_quarantine(proof, [attribution, other], {"exit": [("liability:fill-1", Decimal(200))]})
    by_fee = {raw["id"]: allocations for raw, _, allocations in plans}
    assert by_fee["fee-oversell"] == {"lot-1:fill-1": Decimal("0.05")}  # 1000 / 1200 of 0.07, rounded toward zero
    assert [(s.record_id, s.amount) for s in shares] == [("liability:fill-1", Decimal("-0.02"))]
    plain_only = {"activities": proof["activities"], "cash_fee_populations": {"fee-plain": proof["cash_fee_populations"]["fee-plain"]}}
    assert by_fee["fee-plain"] == allocate_cash_fees(plain_only, [attribution, other])[0][2]
