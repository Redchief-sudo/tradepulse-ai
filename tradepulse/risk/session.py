"""Canonical execution-session guard -- port of
base44/shared/sessionState.ts::executionSessionDecision, WITH the audited
defect fixed.

Confirmed Base44 defect: `if (protectiveExit || side === 'sell') return
{allowed: true, ...}` ran BEFORE the financial-integrity-blocked and
kill-switch checks, so ANY sell order -- not just a genuine reduce-only exit
-- bypassed both safety gates entirely. Here the two hard gates are checked
UNCONDITIONALLY FIRST; the protective-exit exemption only applies afterward,
to the plain "session not active" check.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

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
    if session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED or session.financial_integrity_manual_reenable_required:
        return SessionDecision(False, "FINANCIAL_INTEGRITY_BLOCKED")
    if session.state == SessionState.RISK_STOPPED or session.kill_switch_reset_required:
        return SessionDecision(False, "KILL_SWITCH_ACTIVE")

    if protective_exit:
        return SessionDecision(True, "PROTECTIVE_EXIT_ALLOWED")

    if session.trading_active and session.state == SessionState.MARKET_CLOSED and asset_class == AssetClass.CRYPTO:
        return SessionDecision(True, "CONTINUOUS_ASSET_SESSION")

    if session.state != SessionState.ACTIVE or not session.trading_active:
        return SessionDecision(False, f"TRADING_SESSION_NOT_ACTIVE ({session.state.value})")

    return SessionDecision(True, "ACTIVE")
