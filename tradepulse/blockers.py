"""Everything currently stopping trading, with its cause and how it clears.

Read-only. A blocked system must always be explainable: for each blocker
this reports what is blocked, why, since when, when it was last checked,
whether it clears automatically and, if not, what the operator does.
Sources are the database's own state -- the session latch, integrity holds,
unfinished accounting epochs and intents that block their asset -- so the
report needs no broker access.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from tradepulse.models import SessionState
from tradepulse.persistence import PersistenceRepositories
from tradepulse.persistence.codec import decode_payload
from tradepulse.risk import load_session

# Statuses has_in_flight_intent treats as blocking the asset.
_BLOCKING_INTENT_STATUSES = ("risk_approved", "submitted", "accepted", "partially_filled", "submission_unknown")
_ORDER_BLOCK = "every order on this asset, protective exits included"


@dataclass(frozen=True, slots=True)
class Blocker:
    kind: str
    subject: str
    cause: str
    blocks: str
    since: str | None
    last_checked: str | None
    automatic: bool
    resolution: str

    def as_dict(self) -> dict:
        return asdict(self)


def _session_blocker(session) -> Blocker | None:
    since = session.updated_at.isoformat()
    if session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED:
        return Blocker("session_latch", "session", session.financial_integrity_reason or "financial integrity block",
                       "all new entries (monitor, settlement and reconciliation keep running)", since, None, False,
                       "Investigate the reason, then `tradepulse reset-integrity` (it re-runs reconciliation and "
                       "refuses while drift or a disputed hold remains), then `tradepulse start`.")
    if session.state == SessionState.RISK_STOPPED:
        return Blocker("session_latch", "session", session.kill_switch_reason or "risk kill switch",
                       "all new entries (monitor, settlement and reconciliation keep running)", since, None, False,
                       "`tradepulse reset-risk`, then `tradepulse start`.")
    if session.state in (SessionState.MANUALLY_STOPPED, SessionState.DISABLED):
        return Blocker("session_latch", "session", f"session {session.state.value}",
                       "all new entries (monitor, settlement and reconciliation keep running)", since, None, False,
                       "`tradepulse start` when trading should resume.")
    return None


def _collect(connection: sqlite3.Connection) -> list[Blocker]:
    def last_checked(*subjects: str) -> str | None:
        subjects = tuple(s for s in subjects if s)
        if not subjects:
            return None
        row = connection.execute(
            f"SELECT max(json_extract(payload,'$.occurred_at')) FROM reconciliation_records "
            f"WHERE json_extract(payload,'$.subject_id') IN ({','.join('?' * len(subjects))})", subjects).fetchone()
        return row[0]

    blockers: list[Blocker] = []
    symbols = {row["record_id"]: decode_payload(row["payload"])["asset"]["symbol"]
               for row in connection.execute("SELECT record_id, payload FROM trade_intents")}

    for row in connection.execute("SELECT record_id, status, payload, created_at FROM integrity_holds ORDER BY created_at"):
        hold = decode_payload(row["payload"])
        symbol = symbols.get(hold.get("trade_intent_id"), "?")
        pending = row["status"] == "verification_pending"
        blockers.append(Blocker(
            "integrity_hold", symbol, hold.get("reason") or row["status"],
            f"settlement writes for broker order {row['record_id']}", hold.get("created_at") or row["created_at"],
            hold.get("next_retry_at") and f"attempts {hold.get('attempt_count', 0)}, next retry {hold['next_retry_at']}"
            or last_checked(row["record_id"]),
            pending,
            "Reconciliation re-verifies the order's fill quantity against Alpaca and clears the hold itself."
            if pending else
            "A proven fill-quantity dispute: reconcile the order's fills against Alpaca by hand, then "
            "`tradepulse reset-integrity` (never cleared automatically)."))

    latest: dict[str, sqlite3.Row] = {}
    for row in connection.execute("SELECT status, payload, updated_at FROM accounting_epochs ORDER BY rowid"):
        latest[decode_payload(row["payload"])["canonical_asset_key"]] = row
    for key, row in latest.items():
        if row["status"] == "reconciled_net":
            continue
        epoch = decode_payload(row["payload"])
        reason = epoch.get("reason")
        crypto = key.startswith("crypto:")
        if row["status"] == "integrity_blocked":
            resolution = ("Broker quantity or fees disagree with local lots: investigate, then "
                          "`tradepulse reset-integrity`.")
        elif reason:
            resolution = ("The last finalization attempt failed with the reason shown. Reconciliation retries every "
                          "pass, but it will keep failing until that cause is investigated and corrected.")
        else:
            resolution = "Waiting for Alpaca's fee activities; reconciliation finalizes the epoch when they post."
        blockers.append(Blocker(
            "accounting_epoch", key.split(":")[-1],
            f"accounting {row['status']}" + (f": {reason}" if reason else ""),
            "new entries in this instrument (protective exits still allowed)" if crypto else
            "no orders; risk exposure uses Alpaca's position value until it finalizes",
            epoch.get("opened_at") or row["updated_at"], row["updated_at"],
            row["status"] != "integrity_blocked" and not reason, resolution))

    unresolved = {}
    for row in connection.execute("SELECT payload FROM audit_events WHERE json_extract(payload,'$.event_type')"
                                  "='stranded_intent_unresolved' ORDER BY created_at"):
        event = decode_payload(row["payload"])
        unresolved[event.get("entity_id")] = event
    placeholders = ",".join("?" * len(_BLOCKING_INTENT_STATUSES))
    for row in connection.execute(f"SELECT record_id, status, payload, created_at FROM trade_intents "
                                  f"WHERE status IN ({placeholders}) ORDER BY created_at", _BLOCKING_INTENT_STATUSES):
        intent = decode_payload(row["payload"])
        order_id = intent.get("broker_order_id")
        symbol = intent["asset"]["symbol"]
        checked = last_checked(row["record_id"], order_id)
        if order_id:
            blockers.append(Blocker(
                "order_in_flight", symbol, f"{row['status']} order {order_id} ({intent['side']} {intent.get('requested_quantity')})",
                _ORDER_BLOCK, row["created_at"], checked, True,
                "Reconciliation fetches the order from Alpaca every pass and releases the asset once Alpaca reports "
                "it final; fills are recorded from Alpaca's activities."))
            continue
        event = unresolved.get(row["record_id"])
        if event is not None:
            reason = (event.get("details") or {}).get("reason", "unprovable")
            blockers.append(Blocker(
                "stranded_intent", symbol, f"{row['status']} intent {row['record_id']} cannot be proven ({reason})",
                _ORDER_BLOCK, row["created_at"], checked or event.get("occurred_at"), False,
                "Check Alpaca for an order with this client_order_id. If one exists and is this intent's, or none "
                "exists, resolve the intent by hand; the sweep never guesses."))
            continue
        blockers.append(Blocker(
            "stranded_intent", symbol, f"{row['status']} intent {row['record_id']} has no broker order id",
            _ORDER_BLOCK, row["created_at"], checked, True,
            "The stranded sweep looks the order up by client_order_id each reconciliation pass (after a 120 s "
            "grace): it adopts a proven order, or closes the intent on Alpaca's definitive not-found."))
    return blockers


async def collect_blockers(repositories: PersistenceRepositories) -> list[Blocker]:
    session_blocker = _session_blocker(await load_session(repositories))
    found = await repositories.trade_intents.database.run(_collect)
    return ([session_blocker] if session_blocker else []) + found


async def last_reconciliation_at(repositories: PersistenceRepositories) -> str | None:
    def read(connection: sqlite3.Connection) -> str | None:
        return connection.execute("SELECT max(json_extract(payload,'$.occurred_at')) FROM reconciliation_records").fetchone()[0]
    return await repositories.reconciliation_records.database.run(read)


def format_blockers(blockers: list[Blocker], last_reconciliation: str | None, now: datetime) -> str:
    lines = [f"Last reconciliation pass: {last_reconciliation or 'never'} (now {now.isoformat(timespec='seconds')})"]
    if not blockers:
        lines.append("Nothing is blocking trading.")
        return "\n".join(lines)
    lines.append(f"{len(blockers)} blocker(s):")
    for number, blocker in enumerate(blockers, 1):
        lines += [
            f"{number}. [{blocker.kind}] {blocker.subject}: {blocker.cause}",
            f"   blocks: {blocker.blocks}",
            f"   since: {blocker.since or '?'}   last checked: {blocker.last_checked or 'not yet'}",
            f"   {'clears automatically' if blocker.automatic else 'NEEDS OPERATOR'}: {blocker.resolution}",
        ]
    return "\n".join(lines)
