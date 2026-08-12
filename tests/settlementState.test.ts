import { describe, expect, it, vi } from 'vitest';
import {
  MAX_SETTLEMENT_ATTEMPTS,
  SETTLEMENT_STAGES,
  classifySettlementFailure,
  deriveOrderSettlementSummary,
  isSettlementProcessable,
  runSettlementStages,
  selectLeaseWinner,
  shouldMarkSettlementRecovered,
  summarizeSettlementBatch,
} from '../base44/shared/settlementState.ts';

function handlers(log: string[], failAt?: string) {
  const build = (name: string) => async () => {
    log.push(name);
    if (name === failAt) throw new Error(`temporary ${name} failure`);
    return name === 'projectLot' ? { patch: { realized_pnl: 12.5 } } : undefined;
  };
  return Object.fromEntries([
    'projectLot', 'projectCash', 'projectHolding', 'projectTrade',
    'projectDecision', 'projectIntent', 'verifyIntegrity', 'projectPostSettlement',
  ].map((name) => [name, build(name)]));
}

describe('SettlementEvent stage recovery', () => {
  it('resumes after lot projection instead of treating the event as completed', async () => {
    const stored: Record<string, unknown> = {};
    await expect(runSettlementStages(
      {}, handlers([], 'projectCash'), async (patch: object) => Object.assign(stored, patch)
    )).rejects.toThrow('temporary projectCash failure');
    expect(stored).toMatchObject({ lot_projected: true, realized_pnl: 12.5 });
    expect(stored).not.toHaveProperty('cash_projected');

    const retryLog: string[] = [];
    await runSettlementStages(stored, handlers(retryLog), async (patch: object) => Object.assign(stored, patch));
    expect(retryLog).not.toContain('projectLot');
    expect(retryLog[0]).toBe('projectCash');
    expect(stored.integrity_verified).toBe(true);
  });

  it('projects post-settlement metadata only after integrity verification', async () => {
    const calls: string[] = [];
    const handlers = Object.fromEntries(
      SETTLEMENT_STAGES.map(([, handler]) => [handler, async () => { calls.push(handler); }])
    );
    await runSettlementStages({}, handlers, async () => {});

    expect(calls.indexOf('projectPostSettlement')).toBeGreaterThan(calls.indexOf('verifyIntegrity'));
    expect(calls.slice(-1)).toEqual(['projectPostSettlement']);
  });

  it('does not run post-settlement metadata when integrity verification fails', async () => {
    const calls: string[] = [];
    const handlers = Object.fromEntries(
      SETTLEMENT_STAGES.map(([, handler]) => [handler, async () => {
        calls.push(handler);
        if (handler === 'verifyIntegrity') throw new Error('INTEGRITY_VIOLATION');
      }])
    );

    await expect(runSettlementStages({}, handlers, async () => {})).rejects.toThrow('INTEGRITY_VIOLATION');
    expect(calls).not.toContain('projectPostSettlement');
  });

  it('resumes after cash projection without repeating earlier stages', async () => {
    const log: string[] = [];
    await runSettlementStages(
      { lot_projected: true, cash_projected: true }, handlers(log), vi.fn()
    );
    expect(log[0]).toBe('projectHolding');
    expect(log).not.toContain('projectLot');
    expect(log).not.toContain('projectCash');
  });

  it('does not checkpoint a stage whose projection throws', async () => {
    const checkpoint = vi.fn();
    await expect(runSettlementStages({}, handlers([], 'projectLot'), checkpoint)).rejects.toThrow();
    expect(checkpoint).not.toHaveBeenCalled();
  });

  it('checks lease ownership before each incomplete stage', async () => {
    const guard = vi.fn();
    await runSettlementStages({}, handlers([]), vi.fn(), guard);
    expect(guard).toHaveBeenCalledTimes(SETTLEMENT_STAGES.length);
  });

  it('classifies transient failures as retryable with backoff', () => {
    const failure = classifySettlementFailure({}, new Error('database unavailable'), 1_000);
    expect(failure.status).toBe('retryable_failed');
    expect(failure.attempt_count).toBe(1);
    expect(Date.parse(failure.next_retry_at!)).toBeGreaterThan(1_000);
  });

  it('blocks integrity violations instead of retrying them', () => {
    const failure = classifySettlementFailure({}, new Error('INTEGRITY_VIOLATION: mismatch'));
    expect(failure.status).toBe('integrity_blocked');
    expect(failure.next_retry_at).toBeNull();
  });

  it('makes exhausted transient failures terminal', () => {
    const failure = classifySettlementFailure(
      { attempt_count: MAX_SETTLEMENT_ATTEMPTS - 1 }, new Error('database unavailable')
    );
    expect(failure.status).toBe('terminal_failed');
  });

  it('retries failed events only when their backoff is due', () => {
    expect(isSettlementProcessable({ status: 'retryable_failed', next_retry_at: '2026-01-01T00:00:10Z' }, Date.parse('2026-01-01T00:00:09Z'), 100)).toBe(false);
    expect(isSettlementProcessable({ status: 'retryable_failed', next_retry_at: '2026-01-01T00:00:10Z' }, Date.parse('2026-01-01T00:00:10Z'), 100)).toBe(true);
  });

  it('allows explicit operator recovery to bypass retry backoff', () => {
    expect(isSettlementProcessable({ status: 'retryable_failed', next_retry_at: '2026-01-01T00:00:10Z' }, Date.parse('2026-01-01T00:00:09Z'), 100, true)).toBe(true);
  });

  it('marks a blocked session recovered whenever no settlement remains unresolved', () => {
    expect(shouldMarkSettlementRecovered({
      trading_session_state: 'financial_integrity_blocked',
      financial_integrity_manual_reenable_required: true,
    }, 0)).toBe(true);
    expect(shouldMarkSettlementRecovered({
      trading_session_state: 'disabled',
      financial_integrity_manual_reenable_required: true,
    }, 0)).toBe(true);
  });

  it('never marks recovery while any settlement remains unresolved', () => {
    expect(shouldMarkSettlementRecovered({
      trading_session_state: 'financial_integrity_blocked',
      financial_integrity_manual_reenable_required: true,
    }, 1, true)).toBe(false);
  });

  it('reclaims stale leases but not active leases', () => {
    const event = { status: 'processing', processing_started_at: '2026-01-01T00:00:00Z' };
    expect(isSettlementProcessable(event, Date.parse('2026-01-01T00:00:06Z'), 5_000)).toBe(true);
    expect(isSettlementProcessable(event, Date.parse('2026-01-01T00:00:04Z'), 5_000)).toBe(false);
  });

  it('a fully checkpointed replay performs no projection work', async () => {
    const log: string[] = [];
    await runSettlementStages({
      lot_projected: true, cash_projected: true, holding_projected: true,
      trade_projected: true, decision_projected: true, intent_projected: true,
      integrity_verified: true, post_settlement_projected: true,
    }, handlers(log), vi.fn());
    expect(log).toEqual([]);
  });

  it('serializes concurrent processors with a deterministic lease winner', () => {
    const winner = selectLeaseWinner([
      { id: 'worker-b', acquired_at: '2026-01-01T00:00:00.001Z' },
      { id: 'worker-a', acquired_at: '2026-01-01T00:00:00.000Z' },
    ]);
    expect(winner.id).toBe('worker-a');

    const tieWinner = selectLeaseWinner([
      { id: 'worker-b', acquired_at: '2026-01-01T00:00:00.000Z' },
      { id: 'worker-a', acquired_at: '2026-01-01T00:00:00.000Z' },
    ]);
    expect(tieWinner.id).toBe('worker-a');
  });

  it('keeps a nonterminal partially-filled order distinct from settled fills', () => {
    const summary = deriveOrderSettlementSummary(
      { requested_quantity: 10, status: 'partially_filled' },
      [{ filled_quantity: 4, timestamp: '2026-01-01T00:00:02Z' }]
    );
    expect(summary.orderStatus).toBe('partially_filled');
    expect(summary.settlementState).toBe('current_fills_settled');
  });

  it('derives terminal status and chronology from intent and Fill timestamps', () => {
    const summary = deriveOrderSettlementSummary(
      { requested_quantity: 10, status: 'filled', broker_terminal_status: 'filled' },
      [
        { filled_quantity: 6, timestamp: '2026-01-01T00:00:05Z' },
        { filled_quantity: 4, timestamp: '2026-01-01T00:00:01Z' },
      ]
    );
    expect(summary.orderStatus).toBe('filled');
    expect(summary.settlementState).toBe('settled');
    expect(summary.firstFillAt).toBe('2026-01-01T00:00:01Z');
    expect(summary.lastFillAt).toBe('2026-01-01T00:00:05Z');
  });

  it('does not declare settlement terminal from quantity alone', () => {
    const summary = deriveOrderSettlementSummary(
      { requested_quantity: 10, status: 'partially_filled' },
      [{ filled_quantity: 10, timestamp: '2026-01-01T00:00:01Z' }]
    );
    expect(summary.orderStatus).toBe('partially_filled');
    expect(summary.settlementState).toBe('current_fills_settled');
  });

  it('reports batch failure while any result or settlement remains unresolved', () => {
    expect(summarizeSettlementBatch([{ status: 'completed' }], 0).ok).toBe(true);
    expect(summarizeSettlementBatch([{ status: 'retryable_failed' }], 1)).toMatchObject({ ok: false, failed: 1, unresolved: 1 });
    expect(summarizeSettlementBatch([], 1).ok).toBe(false);
  });
});
