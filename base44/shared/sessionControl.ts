import { nyDayStart } from './marketHours.ts';

export function authorizeSessionAction(user, action, now = new Date(), unresolvedSettlements = 0) {
  if (action === 'start') {
    if (user.financial_integrity_blocked || user.financial_integrity_manual_reenable_required) return { allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' };
    if (user.kill_switch_reset_required || user.trading_session_state === 'risk_stopped') return { allowed: false, reason: 'RISK_STOP_REQUIRES_AUTHORIZED_RESET' };
    if (['system_degraded', 'broker_unavailable'].includes(user.trading_session_state)) return { allowed: false, reason: `SESSION_NOT_READY: ${user.trading_session_state}` };
    return { allowed: true, patch: { trading_active: true, trading_session_state: 'active' } };
  }
  if (action === 'stop') return { allowed: true, patch: { trading_active: false, trading_session_state: 'manually_stopped' } };
  if (action === 'reset_kill_switch') {
    if (!user.kill_switch_reset_required) return { allowed: false, reason: 'KILL_SWITCH_NOT_ACTIVE' };
    if (user.financial_integrity_blocked || user.financial_integrity_manual_reenable_required) return { allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' };
    const triggeredAt = user.kill_switch_at ? new Date(user.kill_switch_at).getTime() : NaN;
    if (!Number.isFinite(triggeredAt) || triggeredAt >= nyDayStart(now).getTime()) return { allowed: false, reason: 'KILL_SWITCH_RESET_BLOCKED_UNTIL_NEXT_TRADING_DAY' };
    return { allowed: true, patch: { trading_active: false, trading_session_state: 'disabled', kill_switch_reset_required: false, kill_switch_reason: null, kill_switch_at: null } };
  }
  if (action === 'acknowledge_integrity_recovery') {
    if (!user.financial_integrity_manual_reenable_required || !user.financial_integrity_recovered_at) return { allowed: false, reason: 'INTEGRITY_RECOVERY_NOT_READY' };
    if (unresolvedSettlements > 0) return { allowed: false, reason: 'UNRESOLVED_SETTLEMENT_EVENTS' };
    return { allowed: true, patch: { trading_active: false, trading_session_state: 'disabled', financial_integrity_blocked: false, financial_integrity_manual_reenable_required: false, financial_integrity_reason: null, financial_integrity_recovered_at: null } };
  }
  return { allowed: false, reason: 'UNSUPPORTED_ACTION' };
}
