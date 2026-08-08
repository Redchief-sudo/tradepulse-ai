import { describe, expect, it } from 'vitest';
import { boundWeights, passesAllGates, validateCandidate } from '../base44/shared/modelGovernance.ts';

const champion = { technical: 40, fundamental: 0, sentiment: 0, momentum: 35, risk: 25 };
const candidate = { technical: 70, fundamental: 0, sentiment: 0, momentum: 20, risk: 10 };

function outcomes(count = 100) {
  return Array.from({ length: count }, (_, index) => ({
    id: `decision-${index}`,
    created_date: new Date(Date.UTC(2026, 0, 1 + index)).toISOString(),
    technical_score: index,
    fundamental_score: 100 - index,
    sentiment_score: index % 11,
    momentum_score: index,
    risk_score: 50,
    realized_return: (index - 50) / 1000,
  }));
}

describe('model governance', () => {
  it('creates disjoint temporal training, validation, and untouched test sets', () => {
    const result = validateCandidate(outcomes(), champion, candidate, 42);
    expect(result.trainingSize).toBe(60);
    expect(result.validationSize).toBe(20);
    expect(result.oosSize).toBe(20);
    const allIds = [...result.trainingIds, ...result.validationIds, ...result.testIds];
    expect(new Set(allIds).size).toBe(100);
    expect(result.folds).toHaveLength(3);
  });

  it('is reproducible for the same dataset and seed', () => {
    expect(validateCandidate(outcomes(), champion, candidate, 73)).toEqual(
      validateCandidate(outcomes(), champion, candidate, 73)
    );
  });

  it('rejects inconsistent walk-forward evidence', () => {
    expect(passesAllGates({ sampleSize: 100, minimumSampleSize: 100, insufficient: false, improvement: 0.1, pValue: 0.01, foldConsistency: 1 / 3, performanceGate: true })).toBe(false);
  });

  it('rejects a challenger that fails risk-adjusted holdout performance', () => {
    expect(passesAllGates({ sampleSize: 100, minimumSampleSize: 100, insufficient: false, improvement: 0.1, pValue: 0.01, foldConsistency: 1, performanceGate: false })).toBe(false);
  });

  it('bounds every factor movement and normalizes weights', () => {
    const bounded = boundWeights(champion, { technical: 90, fundamental: 1, sentiment: 1, momentum: 1, risk: 1 });
    expect(Object.values(bounded).reduce((sum, value) => sum + value, 0)).toBe(100);
    expect(bounded.fundamental).toBe(0);
    expect(bounded.sentiment).toBe(0);
    expect(bounded.technical).toBeGreaterThanOrEqual(32);
    expect(bounded.technical).toBeLessThanOrEqual(48);
  });
});
