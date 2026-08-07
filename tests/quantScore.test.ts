import { describe, it, expect } from 'vitest';
import { weightedComposite, signalFromComposite } from '../base44/shared/quantScore.ts';

describe('weightedComposite', () => {
  it('returns a weighted average of all 5 factors', () => {
    const scores = { technical: 80, fundamental: 60, sentiment: 50, momentum: 70, risk: 40 };
    const weights = { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };
    const result = weightedComposite(scores, weights);
    // (80*25 + 60*25 + 50*20 + 70*15 + 40*15) / 100 = (2000+1500+1000+1050+600)/100 = 61.5
    expect(result).toBeCloseTo(61.5, 1);
  });

  it('defaults missing scores to neutral (50)', () => {
    const scores = { technical: 80 };
    const weights = { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };
    const result = weightedComposite(scores, weights);
    // (80*25 + 50*25 + 50*20 + 50*15 + 50*15) / 100 = (2000+1250+1000+750+750)/100 = 57.5
    expect(result).toBeCloseTo(57.5, 1);
  });

  it('uses default weights when none provided', () => {
    const scores = { technical: 100, fundamental: 100, sentiment: 100, momentum: 100, risk: 100 };
    const result = weightedComposite(scores);
    expect(result).toBeCloseTo(100, 0);
  });

  it('normalizes weights that do not sum to 100', () => {
    const scores = { technical: 80, fundamental: 80, sentiment: 80, momentum: 80, risk: 80 };
    const weights = { technical: 1, fundamental: 1, sentiment: 1, momentum: 1, risk: 1 };
    const result = weightedComposite(scores, weights);
    expect(result).toBeCloseTo(80, 0);
  });
});

describe('signalFromComposite', () => {
  it('returns STRONG_BUY for scores above 80', () => {
    expect(signalFromComposite(85)).toBe('STRONG_BUY');
    expect(signalFromComposite(80.1)).toBe('STRONG_BUY');
  });

  it('returns BUY for scores between 65 and 80', () => {
    expect(signalFromComposite(70)).toBe('BUY');
    expect(signalFromComposite(65.1)).toBe('BUY');
  });

  it('returns HOLD for scores between 45 and 65', () => {
    expect(signalFromComposite(50)).toBe('HOLD');
    expect(signalFromComposite(45.1)).toBe('HOLD');
  });

  it('returns SELL for scores between 30 and 45', () => {
    expect(signalFromComposite(35)).toBe('SELL');
    expect(signalFromComposite(30.1)).toBe('SELL');
  });

  it('returns STRONG_SELL for scores at or below 30', () => {
    expect(signalFromComposite(30)).toBe('STRONG_SELL');
    expect(signalFromComposite(10)).toBe('STRONG_SELL');
  });
});