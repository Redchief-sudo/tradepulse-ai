// Session state machine — the authoritative trading state.
//
// States:
//   disabled         — user has not started trading
//   active           — scanning and executing normally
//   risk_stopped     — kill switch tripped (daily loss, broker drawdown)
//   system_degraded  — scan failure, data stale, heartbeat missing
//   broker_unavailable — broker API unreachable or account invalid
//   market_closed    — outside market hours (stocks only; crypto is 24/7)
//   manually_stopped — user pressed Stop
//   financial_integrity_blocked — settlement integrity check failed; new entries blocked, protective exits only
//
// The kill switch (risk_stopped) requires manual reset — the system does NOT
// auto-re-enable on the same day. The user must acknowledge the stop and
// explicitly restart.
//
// FAIL-CLOSED: state persistence errors are NOT swallowed. If the database
// update fails, the caller receives an exception and must handle it. This
// prevents a kill-switch failure from being silently ignored while trading
// continues. (Fixes Rev.12 audit defect #25: session-state persistence could
// fail silently, leaving trading_active true after a kill switch.)

export const SESSION_STATES = {
  DISABLED: 'disabled',
  ACTIVE: 'active',
  RISK_STOPPED: 'risk_stopped',
  SYSTEM_DEGRADED: 'system_degraded',
  BROKER_UNAVAILABLE: 'broker_unavailable',
  MARKET_CLOSED: 'market_closed',
  MANUALLY_STOPPED: 'manually_stopped',
  FINANCIAL_INTEGRITY_BLOCKED: 'financial_integrity_blocked',
};

// Update the user's session state. Called by the scan cycle, stop-loss cycle,
// and the UI. Persists the state and optional reason/timestamp.
// THROWS on failure — callers must catch and handle appropriately.
export async function updateSessionState(sr, userId, newState, reason = null) {
  const patch = { trading_session_state: newState };
  if (newState === SESSION_STATES.RISK_STOPPED) {
    patch.kill_switch_reason = reason;
    patch.kill_switch_at = new Date().toISOString();
    patch.kill_switch_reset_required = true;
    patch.trading_active = false;
  }
  if (newState === SESSION_STATES.DISABLED || newState === SESSION_STATES.MANUALLY_STOPPED || newState === SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED) {
    patch.trading_active = false;
  }
  if (newState === SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED) {
    patch.financial_integrity_reason = reason;
    patch.financial_integrity_recovered_at = null;
    patch.financial_integrity_manual_reenable_required = true;
  }
  if (newState === SESSION_STATES.ACTIVE) {
    // Resetting the kill switch when manually re-activating
    patch.kill_switch_reset_required = false;
    patch.kill_switch_reason = null;
    patch.kill_switch_at = null;
    patch.trading_active = true;
    patch.financial_integrity_reason = null;
    patch.financial_integrity_recovered_at = null;
    patch.financial_integrity_manual_reenable_required = false;
  }
  // FAIL-CLOSED: throw on persistence failure. The caller must handle the
  // error — for risk_stopped, a failed update means the kill switch may not
  // be active, which is a critical safety failure. (Fixes Rev.12 #25.)
  await sr.entities.User.update(userId, patch);
}

// Check if the session is in a tradeable state.
export function isTradeable(sessionState) {
  return sessionState === SESSION_STATES.ACTIVE;
}

// Canonical execution-session guard. New exposure requires an explicitly
// active, integrity-healthy session. Protective sells remain available while
// stopped so risk can be reduced without reopening exposure.
export function executionSessionDecision(user, side) {
  if (side === 'sell') return { allowed: true, reason: 'PROTECTIVE_EXIT_ALLOWED' };
  if (user?.financial_integrity_manual_reenable_required ||
      user?.trading_session_state === SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED) {
    return { allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' };
  }
  if (user?.kill_switch_reset_required || user?.trading_session_state === SESSION_STATES.RISK_STOPPED) {
    return { allowed: false, reason: 'KILL_SWITCH_ACTIVE' };
  }
  if (user?.trading_session_state !== SESSION_STATES.ACTIVE || !user?.trading_active) {
    return { allowed: false, reason: `TRADING_SESSION_NOT_ACTIVE (${user?.trading_session_state || 'unknown'})` };
  }
  return { allowed: true, reason: 'ACTIVE' };
}

// Check if the session is in a stopped state (any non-active, non-disabled state).
export function isStopped(sessionState) {
  return ![SESSION_STATES.ACTIVE, SESSION_STATES.DISABLED].includes(sessionState);
}
