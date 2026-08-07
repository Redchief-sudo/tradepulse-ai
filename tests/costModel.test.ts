import { describe, it, expect } from 'vitest';
import { netEdge, estimateCosts, costModelFor } from '../base44/shared/costModel.ts';

describe('netEdge', () => {
  it('returns negative when gross return is less than round-trip costs', () => {
    // Stocks: round-trip = (0 + 5 + 2) * 2 = 14 bps = 0.14%
    expect(netEdge(0.1, 'stocks')).toBeLessThan(0);
  });

  it('returns positive when gross return exceeds round-trip costs', () => {
    expect(netEdge(1.0, 'stocks')).toBeGreaterThan(0);
  });

  it('returns exactly zero at the breakeven threshold', () => {
    // Stocks round-trip cost = 0.14%
    expect(netEdge(0.14, 'stocks')).toBeCloseTo(0, 2);
  });

  it('crypto has higher costs than stocks', () => {
    expect(netEdge(1.0, 'crypto')).toBeLessThan(netEdge(1.0, 'stocks'));
  });

  it('falls back to stock costs for unknown asset classes', () => {
    expect(netEdge(1.0, 'unknown')).toBeCloseTo(netEdge(1.0, 'stocks'), 5);
  });
});

describe('estimateCosts', () => {
  it('computes one-way and round-trip costs from notional', () => {
    const result = estimateCosts('stocks', 10000);
    // Stocks: one-way = 7 bps, round-trip = 14 bps
    expect(result.one_way_bps).toBe(7);
    expect(result.round_trip_bps).toBe(14);
    expect(result.one_way_cost).toBeCloseTo(7, 1);   // 7bps of 10000 = $7
    expect(result.round_trip_cost).toBeCloseTo(14, 1); // 14bps of 10000 = $14
  });

  it('round-trip is always double one-way', () => {
    for (const ac of ['stocks', 'crypto', 'forex', 'commodities', 'fixed_income']) {
      const r = estimateCosts(ac, 50000);
      expect(r.round_trip_bps).toBe(r.one_way_bps * 2);
      expect(r.round_trip_cost).toBeCloseTo(r.one_way_cost * 2, 1);
    }
  });
});

describe('costModelFor', () => {
  it('returns stock model for unknown asset classes', () => {
    expect(costModelFor('unknown')).toBe(costModelFor('stocks'));
  });
});