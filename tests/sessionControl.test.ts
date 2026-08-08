import { describe, expect, it } from 'vitest';
import { authorizeSessionAction } from '../base44/shared/sessionControl.ts';

describe('authoritative session control', () => {
  it('does not let start clear a kill switch or integrity block', () => {
    expect(authorizeSessionAction({ kill_switch_reset_required: true }, 'start').allowed).toBe(false);
    expect(authorizeSessionAction({ financial_integrity_blocked: true }, 'start').allowed).toBe(false);
  });

  it('blocks same-trading-day kill-switch reset', () => {
    const now = new Date('2026-08-07T18:00:00Z');
    const result = authorizeSessionAction({ kill_switch_reset_required: true, kill_switch_at: '2026-08-07T15:00:00Z' }, 'reset_kill_switch', now);
    expect(result).toMatchObject({ allowed: false, reason: 'KILL_SWITCH_RESET_BLOCKED_UNTIL_NEXT_TRADING_DAY' });
  });

  it('allows an expired kill switch reset but keeps trading disabled', () => {
    const result = authorizeSessionAction({ kill_switch_reset_required: true, kill_switch_at: '2026-08-06T15:00:00Z' }, 'reset_kill_switch', new Date('2026-08-07T18:00:00Z'));
    expect(result).toMatchObject({ allowed: true, patch: { trading_active: false, trading_session_state: 'disabled', kill_switch_reset_required: false } });
  });

  it('requires recovered integrity and zero unresolved settlements', () => {
    const user = { financial_integrity_manual_reenable_required: true, financial_integrity_recovered_at: '2026-08-07T17:00:00Z' };
    expect(authorizeSessionAction(user, 'acknowledge_integrity_recovery', new Date(), 1).allowed).toBe(false);
    expect(authorizeSessionAction(user, 'acknowledge_integrity_recovery', new Date(), 0).allowed).toBe(true);
  });
});
