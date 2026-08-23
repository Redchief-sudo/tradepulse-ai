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

from dataclasses import dataclass
from datetime import UTC, datetime

from tradepulse.models import AssetClass, SessionState, Side, TradingSession
from tradepulse.persistence import PersistenceRepositories, hydrate

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
