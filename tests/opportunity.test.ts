import { describe, expect, it } from 'vitest';
import { canonicalizeOpportunity, marketSession, normalizeAssetClass } from '../base44/shared/opportunity.ts';

const receivedAt = '2026-08-07T18:00:01.000Z';

describe('canonical opportunity contract', () => {
  it('normalizes equity identity and explicitly allows a liquid paper symbol', () => {
    const result = canonicalizeOpportunity(
      { asset_class: 'stocks', symbol: 'aapl' },
      { price: 220, quote_timestamp: Date.parse(receivedAt) / 1000, venue: 'finnhub', market: 'US', source: 'finnhub' },
      receivedAt,
      'paper',
    );
    expect(result).toMatchObject({ asset_class: 'equities', native_symbol: 'AAPL', canonical_symbol: 'AAPL', execution_capability: 'paper_executable' });
  });

  it('keeps a crypto pair native and research-only when execution is not declared', () => {
    const result = canonicalizeOpportunity(
      { asset_class: 'crypto', symbol: 'BTC-USD' },
      { price: 100000, bid: 99999, ask: 100001, quote_timestamp: Date.parse(receivedAt) / 1000, venue: 'coinbase', market: 'BTC-USD', source: 'coinbase_exchange' },
      receivedAt,
    );
    expect(result).toMatchObject({ native_symbol: 'BTC-USD', canonical_symbol: 'BTCUSD', market_session: 'continuous', execution_capability: 'research_only', spread: 2 });
  });

  it.each([
    ['forex', 'EURUSD=X'],
    ['commodities', 'GC=F'],
    ['unsupported', 'MYSTERY'],
  ])('fails closed for unsupported %s instruments', (assetClass, symbol) => {
    const result = canonicalizeOpportunity({ asset_class: assetClass, symbol }, { error: 'unsupported' }, receivedAt);
    expect(result.execution_capability).toBe('unsupported');
    expect(result.price).toBeNull();
    expect(result.provider_timestamp).toBeNull();
  });

  it('never substitutes receipt time for a missing provider timestamp', () => {
    const result = canonicalizeOpportunity({ asset_class: 'stocks', symbol: 'AAPL' }, { price: 220 }, receivedAt);
    expect(result.execution_capability).toBe('unsupported');
    expect(result.provider_timestamp).toBeNull();
  });

  it('does not mark stale provider data executable', () => {
    const result = canonicalizeOpportunity(
      { asset_class: 'stocks', symbol: 'AAPL' },
      { price: 220, quote_timestamp: Date.parse('2026-08-07T17:00:00.000Z') / 1000 },
      receivedAt,
    );
    expect(result.execution_capability).toBe('unsupported');
    expect(result.provider_error).toBe('stale provider timestamp');
  });

  it('uses asset-specific sessions', () => {
    expect(marketSession('crypto', '2026-08-08T12:00:00Z')).toBe('continuous');
    expect(marketSession('equities', '2026-08-08T12:00:00Z')).toBe('closed');
  });

  it('normalizes only declared asset classes', () => {
    expect(normalizeAssetClass('futures')).toBe('commodities');
    expect(normalizeAssetClass('options')).toBe('unsupported');
  });
});
