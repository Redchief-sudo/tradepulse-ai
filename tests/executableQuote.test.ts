import { describe, expect, it } from 'vitest';
import { executableQuoteMetrics, executionLifecycle, defaultTimeInForce } from '../base44/shared/execution.ts';
import { inferAlpacaAssetClass, normalizeAlpacaActivitySide, normalizeAlpacaSymbol } from '../base44/shared/alpaca.ts';

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

describe('execution lifecycle truth', () => {
  it('keeps a broker fill financially incomplete while settlement is pending', () => {
    expect(executionLifecycle('filled', 'pending')).toMatchObject({ status: 'settlement_pending', broker_status: 'filled', settlement_status: 'pending', financially_complete: false, order_execution_complete: true, fills_settlement_complete: false });
  });
  it('marks completion only after completed settlement', () => {
    expect(executionLifecycle('filled', 'completed').financially_complete).toBe(true);
  });
  it('separates settled partial fills from terminal order completion', () => {
    expect(executionLifecycle('partially_filled', 'completed')).toMatchObject({ financially_complete: false, order_execution_complete: false, fills_settlement_complete: true });
  });
});

describe('Alpaca activity direction', () => {
  it('maps short and cover activities to canonical signed-ledger sides', () => {
    expect(normalizeAlpacaActivitySide('sell_short')).toBe('sell');
    expect(normalizeAlpacaActivitySide('buy_to_cover')).toBe('buy');
  });
});

describe('Alpaca crypto execution identity', () => {
  it('normalizes scan and broker pair formats to one symbol', () => {
    expect(normalizeAlpacaSymbol('BTC-USD', 'crypto')).toBe('BTC/USD');
    expect(normalizeAlpacaSymbol('BTCUSD', 'crypto')).toBe('BTC/USD');
    expect(normalizeAlpacaSymbol('AAPL', 'stocks')).toBe('AAPL');
  });

  it('recognizes compact Alpaca crypto activity symbols', () => {
    expect(inferAlpacaAssetClass('ETHUSD')).toBe('crypto');
    expect(inferAlpacaAssetClass('MSFT')).toBe('stocks');
  });

  it('uses a crypto-valid persistent time in force by default', () => {
    expect(defaultTimeInForce('crypto')).toBe('gtc');
    expect(defaultTimeInForce('stocks')).toBe('day');
  });
});
