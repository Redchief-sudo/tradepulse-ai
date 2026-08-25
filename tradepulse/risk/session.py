"""Canonical execution-session guard -- port of
base44/shared/sessionState.ts::executionSessionDecision, WITH the audited
defect fixed.

Confirmed Base44 defect: `if (protectiveExit || side === 'sell') return
{allowed: true, ...}` ran BEFORE the financial-integrity-blocked and
kill-switch checks, so ANY sell order -- not just a genuine reduce-only exit
-- bypassed both safety gates entirely. That bug was in how the exemption
was COMPUTED (any `side === 'sell'`), not in checking it first. Here
`protective_exit` is instead a narrowly-computed boolean the caller derives
from actual position state (see execution/gateway.py: selling no more than
the held quantity, or a buy that closes a short) -- so it's safe to check
FIRST, before either hard gate: a kill-switch/integrity block must stop new
risk-taking, but must not also freeze a position's own protective
stop-loss/target exit while it's already blocked from opening anything new.
A non-protective sell (protective_exit=False) still falls through to both
hard gates exactly as before.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from tradepulse.models import AssetClass, AuditEvent, SessionState, Side, TradingSession
from tradepulse.persistence import PersistenceRepositories, hydrate
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.persistence.repositories import utc_now

# Single-operator system: exactly one TradingSession row exists, at this
# fixed record_id.
SESSION_RECORD_ID = "session"


async def load_session(repositories: PersistenceRepositories) -> TradingSession:
    row = await repositories.trading_sessions.get(SESSION_RECORD_ID)
    if row is None:
        return TradingSession(SESSION_RECORD_ID, SessionState.DISABLED, False, datetime.now(UTC))
    return hydrate("trading_sessions", row["payload"])


async def save_session(repositories: PersistenceRepositories, session: TradingSession) -> None:
    existing = await repositories.trading_sessions.get(SESSION_RECORD_ID)
    if existing is None:
        await repositories.trading_sessions.create_once(SESSION_RECORD_ID, session, status=session.state.value)
    else:
        await repositories.trading_sessions.update(SESSION_RECORD_ID, session, status=session.state.value)


async def transition_session(
    repositories: PersistenceRepositories,
    decide: Callable[[TradingSession], tuple[TradingSession, AuditEvent] | None],
) -> TradingSession | None:
    """Atomically re-read the singleton TradingSession row, call
    decide(current) for a final go/no-go against the FRESH state (not
    whatever the caller read before this call), and if it returns a new
    session + audit event, write BOTH in the same BEGIN IMMEDIATE
    transaction -- so a session-state change and its audit record always
    commit together or not at all, and a concurrent second command (e.g.
    `start` racing `stop`) re-decides against whatever actually got
    committed first rather than a stale pre-transaction read. Same
    serialization guarantee persistence/lock.py::acquire_lock and
    RecordRepository.claim_if_processable already rely on. Returns the new
    session, or None if decide refused (state changed concurrently, or
    nothing to do) -- in which case nothing is written, including no audit
    event for a no-op.

    Any async precondition (a live broker health check, a live
    reconciliation pass) must happen BEFORE calling this -- the transaction
    body below is synchronous, SQLite-only work, matching every other
    database.run(write=True) caller in this codebase. `decide`'s closure
    can capture such a precondition's result, but must still re-validate
    `current.state` against what's actually committed here."""
    database = repositories.trading_sessions.database

    def op(connection: sqlite3.Connection) -> TradingSession | None:
        row = connection.execute("SELECT * FROM trading_sessions WHERE record_id=?", (SESSION_RECORD_ID,)).fetchone()
        current = (
            hydrate("trading_sessions", decode_payload(row["payload"])) if row is not None
            else TradingSession(SESSION_RECORD_ID, SessionState.DISABLED, False, datetime.now(UTC))
        )
        decision = decide(current)
        if decision is None:
            return None
        new_session, event = decision
        now = utc_now()
        if row is None:
            connection.execute(
                "INSERT INTO trading_sessions (record_id, status, payload, created_at, updated_at) VALUES (?,?,?,?,?)",
                (SESSION_RECORD_ID, new_session.state.value, encode_payload(new_session), now, now),
            )
        else:
            connection.execute(
                "UPDATE trading_sessions SET status=?, payload=?, updated_at=? WHERE record_id=?",
                (new_session.state.value, encode_payload(new_session), now, SESSION_RECORD_ID),
            )
        connection.execute(
            "INSERT INTO audit_events (record_id, payload, created_at) VALUES (?,?,?)",
            (event.event_id, encode_payload(event), now),
        )
        return new_session

    return await database.run(op, write=True)


@dataclass(frozen=True, slots=True)
class SessionDecision:
    allowed: bool
    reason: str


def execution_session_decision(
    session: TradingSession, side: Side, asset_class: AssetClass, protective_exit: bool
) -> SessionDecision:
    """`protective_exit` must be computed by the caller from actual position
    state (selling <= held quantity, or a buy that closes a short) -- never
    inferred from `side` alone.
    """
    if protective_exit:
        return SessionDecision(True, "PROTECTIVE_EXIT_ALLOWED")

    if session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED or session.financial_integrity_manual_reenable_required:
        return SessionDecision(False, "FINANCIAL_INTEGRITY_BLOCKED")
    if session.state == SessionState.RISK_STOPPED or session.kill_switch_reset_required:
        return SessionDecision(False, "KILL_SWITCH_ACTIVE")

    if session.trading_active and session.state == SessionState.MARKET_CLOSED and asset_class == AssetClass.CRYPTO:
        return SessionDecision(True, "CONTINUOUS_ASSET_SESSION")

    if session.state != SessionState.ACTIVE or not session.trading_active:
        return SessionDecision(False, f"TRADING_SESSION_NOT_ACTIVE ({session.state.value})")

    return SessionDecision(True, "ACTIVE")


async def latch_risk_stop(
    repositories: PersistenceRepositories, reason: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TradingSession | None:
    """Atomically transition into RISK_STOPPED from a genuine account-level
    kill-switch condition (daily-loss / max-drawdown breach) -- not an
    ordinary per-trade risk rejection. Idempotent (a no-op if already
    RISK_STOPPED, preserving the original reason/timestamp rather than
    overwriting it on a later breach) and never downgrades an existing
    FINANCIAL_INTEGRITY_BLOCKED, which is the more severe condition."""
    now = clock()

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state in (SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED):
            return None
        new_session = TradingSession(
            SESSION_RECORD_ID, SessionState.RISK_STOPPED, False, now,
            kill_switch_reason=reason, kill_switch_at=now, kill_switch_reset_required=True,
        )
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="critical",
            message=f"{session.state.value} -> risk_stopped: {reason}", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={"action": "latch_risk_stop", "previous_state": session.state.value, "new_state": "risk_stopped", "reason": reason},
        )
        return new_session, event

    return await transition_session(repositories, decide)


async def latch_financial_integrity_block(
    repositories: PersistenceRepositories, reason: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TradingSession | None:
    """Atomically transition into FINANCIAL_INTEGRITY_BLOCKED from a genuine
    settlement/accounting-truth failure (SettlementStatus.INTEGRITY_BLOCKED,
    reconciliation's ACCOUNTING_DRIFT, or an unrecoverable/orphan broker
    fill). Idempotent for itself, but -- unlike latch_risk_stop -- DOES
    escalate out of an existing RISK_STOPPED: an accounting-integrity
    problem is more severe than an ordinary risk-limit breach and needs its
    own, more rigorous reset path (reset-integrity's reconciliation gate);
    it must never be silently absorbed into a plain risk stop that
    reset-risk alone could clear."""
    now = clock()

    def decide(session: TradingSession) -> tuple[TradingSession, AuditEvent] | None:
        if session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED:
            return None
        new_session = TradingSession(
            SESSION_RECORD_ID, SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, now,
            financial_integrity_reason=reason, financial_integrity_manual_reenable_required=True,
        )
        event = AuditEvent(
            event_id=str(uuid4()), event_type="session_transition", severity="critical",
            message=f"{session.state.value} -> financial_integrity_blocked: {reason}", occurred_at=now,
            entity_type="trading_session", entity_id=SESSION_RECORD_ID,
            details={"action": "latch_integrity_block", "previous_state": session.state.value, "new_state": "financial_integrity_blocked", "reason": reason},
        )
        return new_session, event

    return await transition_session(repositories, decide)
