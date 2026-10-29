"""Signed fee allocation and conserved recognition for quarantined records (Rev.140, spec §3.6).

Allocation (I4): each posted fee, signed (a charge negative, a reversal or
credit positive), is split across the order's closure attributions and any
quarantined record its execution created, in proportion to proceeds. Shares
round toward zero at the fee's own quantum; quarantined records come last,
so the final one takes the exact remainder.

Recognition (C1, per record and pool): A = R + U, where A is Σ allocations,
R is Σ recognitions and U the unrecognized balance, with U = 0 whenever the
record's open quantity is 0.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from tradepulse.models.quarantine import QuarantineFeeAllocation, QuarantineFeeRecognition
from tradepulse.persistence import PersistenceRepositories, hydrate, paginate_all_rows
from tradepulse.persistence.codec import decode_payload, encode_payload

Pool = Literal["opening", "cover"]


class BrokerEvidenceChanged(ValueError):
    """The same fee ID arrived with a different amount (BROKER_EVIDENCE_CHANGED)."""


def _quantum(amount: Decimal) -> Decimal:
    return Decimal(1).scaleb(min(amount.as_tuple().exponent, -2))  # type: ignore[arg-type]


def allocate_fee(fee_canonical_id: str, signed_amount: Decimal, targets: Sequence[tuple[str, Decimal]],
                 quarantined: Sequence[tuple[str, Decimal, Pool]]) -> list[QuarantineFeeAllocation]:
    """Every share of one fee: attributions first (pool 'opening'), then the
    quarantined records, the last of which takes the exact remainder."""
    ordered: list[tuple[str, Decimal, Pool]] = [(rid, proceeds, "opening") for rid, proceeds in targets] + list(quarantined)
    total = sum((proceeds for _, proceeds, _ in ordered), Decimal(0))
    if not ordered or total <= 0 or any(proceeds < 0 for _, proceeds, _ in ordered):
        raise ValueError("QUARANTINE_FEE_ALLOCATION_INVALID")
    quantum = _quantum(signed_amount)
    remaining = signed_amount
    shares = []
    for index, (record_id, proceeds, pool) in enumerate(ordered):
        amount = remaining if index == len(ordered) - 1 else (signed_amount * proceeds / total).quantize(quantum, rounding=ROUND_DOWN)
        shares.append(QuarantineFeeAllocation(fee_canonical_id=fee_canonical_id, record_id=record_id, pool=pool, amount=amount))
        remaining -= amount
    return shares


def recognize_at_resolution(record_id: str, pool: Pool, *, open_before: Decimal, covered: Decimal, unrecognized: Decimal,
                            event_id: str, now: datetime) -> QuarantineFeeRecognition:
    """⌊U x q / q0⌋ toward zero; exactly U when the resolution closes the record.
    The share is of the current unrecognized balance, so a correction between
    partial resolutions is spread over what remains and nothing is recognised twice."""
    if covered <= 0 or covered > open_before:
        raise ValueError("QUARANTINE_RESOLUTION_QUANTITY_INVALID")
    amount = unrecognized if covered == open_before else (unrecognized * covered / open_before).quantize(
        _quantum(unrecognized), rounding=ROUND_DOWN)
    return QuarantineFeeRecognition(record_id=record_id, event_id=event_id, pool=pool, amount=amount, recognized_at=now)


def recognize_adjustment(record_id: str, pool: Pool, amount: Decimal, event_id: str, posted_at: datetime) -> QuarantineFeeRecognition:
    """A dated adjustment: a cover fee posting after its resolution, or an
    allocation arriving once the record is fully resolved. Recognised in full."""
    return QuarantineFeeRecognition(record_id=record_id, event_id=event_id, pool=pool, amount=amount, recognized_at=posted_at)


def insert_allocations(connection: sqlite3.Connection, shares: Sequence[QuarantineFeeAllocation], created_at: str) -> None:
    """Append-only; a replay is a no-op and a different amount for the same
    fee and record is BROKER_EVIDENCE_CHANGED. Attribution shares are not
    quarantine evidence and are skipped."""
    for share in shares:
        if share.record_id.startswith("attribution:"):
            continue
        key = f"{share.fee_canonical_id}|{share.record_id}"
        row = connection.execute("SELECT payload FROM quarantine_fee_allocations WHERE allocation_key=?", (key,)).fetchone()
        if row is not None:
            if hydrate("quarantine_fee_allocations", decode_payload(row[0])).amount != share.amount:
                raise BrokerEvidenceChanged(f"BROKER_EVIDENCE_CHANGED: {key}")
            continue
        connection.execute("INSERT INTO quarantine_fee_allocations (record_id,allocation_key,payload,created_at) VALUES (?,?,?,?)",
                           (key, key, encode_payload(share), created_at))


async def record_allocations(repositories: PersistenceRepositories, shares: Sequence[QuarantineFeeAllocation]) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await repositories.quarantine_fee_allocations.database.run(lambda c: insert_allocations(c, shares, now), write=True)


async def record_recognition(repositories: PersistenceRepositories, recognition: QuarantineFeeRecognition) -> None:
    key = f"{recognition.record_id}|{recognition.event_id}|{recognition.pool}"
    await repositories.quarantine_fee_recognitions.create_once(key, recognition, unique_value=key)


async def conservation(repositories: PersistenceRepositories, record_id: str, pool: Pool) -> tuple[Decimal, Decimal, Decimal]:
    """(A, R, U) for one record and pool."""
    allocated = sum((share.amount for share in (hydrate("quarantine_fee_allocations", row["payload"])
                                                for row in await paginate_all_rows(repositories.quarantine_fee_allocations))
                     if share.record_id == record_id and share.pool == pool), Decimal(0))
    recognized = sum((item.amount for item in (hydrate("quarantine_fee_recognitions", row["payload"])
                                               for row in await paginate_all_rows(repositories.quarantine_fee_recognitions))
                      if item.record_id == record_id and item.pool == pool), Decimal(0))
    return allocated, recognized, allocated - recognized


async def check_c1(repositories: PersistenceRepositories) -> list[str]:
    """`record|pool` for every record and pool with U != 0 while the record is
    fully resolved. A = R + U holds by construction; the open-quantity rule
    is what can fail."""
    from tradepulse.quarantine import quarantined_positions

    open_records = {record.record_id for record in await quarantined_positions(repositories)}
    pools = {(share.record_id, share.pool) for share in (hydrate("quarantine_fee_allocations", row["payload"])
                                                         for row in await paginate_all_rows(repositories.quarantine_fee_allocations))}
    violations = []
    for record_id, pool in sorted(pools):
        _, _, unrecognized = await conservation(repositories, record_id, pool)
        if unrecognized != 0 and (record_id not in open_records or pool == "cover"):
            violations.append(f"{record_id}|{pool}")
    return violations
