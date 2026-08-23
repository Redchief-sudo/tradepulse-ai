import { describe, expect, it } from 'vitest';
import { executionSessionDecision } from '../base44/shared/sessionState.ts';

describe('canonical execution session gate', () => {
  it('allows new exposure only for an explicitly active healthy session', () => {
    expect(executionSessionDecision({ trading_active: true, trading_session_state: 'active' }, 'buy'))
      .toEqual({ allowed: true, reason: 'ACTIVE' });
  });

  it.each([
    ['disabled', false],
    ['manually_stopped', false],
    ['system_degraded', false],
    ['broker_unavailable', false],
    ['market_closed', false],
  ])('rejects buys in %s state', (trading_session_state, trading_active) => {
    expect(executionSessionDecision({ trading_active, trading_session_state }, 'buy').allowed).toBe(false);
  });

  it('rejects buys while financial integrity requires manual re-enable', () => {
    const decision = executionSessionDecision({
      trading_active: false,
      trading_session_state: 'financial_integrity_blocked',
      financial_integrity_manual_reenable_required: true,
    }, 'buy');
    expect(decision).toEqual({ allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' });
  });

  it('rejects buys while the kill switch requires reset', () => {
    expect(executionSessionDecision({
      trading_active: false,
      trading_session_state: 'risk_stopped',
      kill_switch_reset_required: true,
    }, 'buy')).toEqual({ allowed: false, reason: 'KILL_SWITCH_ACTIVE' });
  });

  it('rejects sells while financial integrity requires manual re-enable — the kill-switch/integrity hard stops are unconditional and run before the protective-exit shortcut', () => {
    expect(executionSessionDecision({
      trading_active: false,
      trading_session_state: 'financial_integrity_blocked',
      financial_integrity_manual_reenable_required: true,
    }, 'sell')).toEqual({ allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' });
  });

  it('rejects an explicitly classified buy-to-cover while integrity is blocked', () => {
    expect(executionSessionDecision({
      trading_active: false,
      trading_session_state: 'financial_integrity_blocked',
      financial_integrity_manual_reenable_required: true,
    }, 'buy', 'stocks', true)).toEqual({ allowed: false, reason: 'FINANCIAL_INTEGRITY_BLOCKED' });
  });

  it('rejects sells while the kill switch requires reset — not just buys', () => {
    expect(executionSessionDecision({
      trading_active: false,
      trading_session_state: 'risk_stopped',
      kill_switch_reset_required: true,
    }, 'sell')).toEqual({ allowed: false, reason: 'KILL_SWITCH_ACTIVE' });
  });

  it('still allows protective sells during a plain inactive/market-closed state (not integrity- or kill-switch-blocked)', () => {
    expect(executionSessionDecision({
      trading_active: false,
      trading_session_state: 'manually_stopped',
    }, 'sell')).toEqual({ allowed: true, reason: 'PROTECTIVE_EXIT_ALLOWED' });
  });

  it('allows crypto but not equities after the US market closes', () => {
    const user = { trading_active: true, trading_session_state: 'market_closed' };
    expect(executionSessionDecision(user, 'buy', 'crypto')).toEqual({ allowed: true, reason: 'CONTINUOUS_ASSET_SESSION' });
    expect(executionSessionDecision(user, 'buy', 'equities').allowed).toBe(false);
  });
});
