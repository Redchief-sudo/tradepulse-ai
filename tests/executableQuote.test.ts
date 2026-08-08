import { describe, expect, it } from 'vitest';
import { executableQuoteMetrics } from '../base44/shared/execution.ts';

describe('executableQuoteMetrics', () => {
  const now = Date.parse('2026-08-07T17:00:01.000Z');
  const quote = { bid: 99.9, ask: 100.1, timestamp: '2026-08-07T17:00:00.000Z' };

  it('uses the ask for buys and computes half-spread slippage', () => {
    const result = executableQuoteMetrics(quote, 'buy', now);
    expect(result?.referencePrice).toBe(100.1);
    expect(result?.estimatedSlippagePct).toBeCloseTo(0.1, 8);
    expect(result?.ageMs).toBe(1000);
  });

  it('uses the bid for sells', () => {
    expect(executableQuoteMetrics(quote, 'sell', now)?.referencePrice).toBe(99.9);
  });

  it('rejects crossed and non-positive books', () => {
    expect(executableQuoteMetrics({ bid: 101, ask: 100 }, 'buy', now)).toBeNull();
    expect(executableQuoteMetrics({ bid: 0, ask: 100 }, 'buy', now)).toBeNull();
  });

  it('does not invent a timestamp when the provider omits it', () => {
    expect(executableQuoteMetrics({ bid: 99, ask: 100 }, 'buy', now)?.ageMs).toBeNull();
  });
});
