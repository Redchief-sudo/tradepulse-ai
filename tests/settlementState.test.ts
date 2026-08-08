import { describe, expect, it, vi } from 'vitest';
import {
  MAX_SETTLEMENT_ATTEMPTS,
  classifySettlementFailure,
  isSettlementProcessable,
  runSettlementStages,
  selectLeaseWinner,
} from '../base44/shared/settlementState.ts';

function handlers(log: string[], failAt?: string) {
  const build = (name: string) => async () => {
    log.push(name);
    if (name === failAt) throw new Error(`temporary ${name} failure`);
    return name === 'projectLot' ? { patch: { realized_pnl: 12.5 } } : undefined;
  };
  return Object.fromEntries([
    'projectLot', 'projectCash', 'projectHolding', 'projectTrade',
    'projectDecision', 'projectIntent', 'verifyIntegrity',
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
      integrity_verified: true,
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
});
