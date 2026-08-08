import { describe, expect, it } from 'vitest';
import { boundWeights, collectOutcomes, getExactChampion, passesAllGates, runGovernanceCycle, strategyMetrics, validateCandidate } from '../base44/shared/modelGovernance.ts';

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

  it('computes compounded portfolio maximum drawdown for promotion metrics', () => {
    const rows = [0.1, -0.1, -0.1, 0.01, 0.01, 0.01].map((realized_return, index) => ({
      created_date: new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
      technical_score: 100 - index,
      momentum_score: 100 - index,
      risk_score: 50,
      realized_return,
    }));
    const metrics = strategyMetrics(rows, champion);
    expect(metrics.maxDrawdown).toBeGreaterThan(0);
    expect(metrics.maxDrawdown).not.toBe(metrics.worstTradeLoss);
  });

  it('bounds every factor movement and normalizes weights', () => {
    const bounded = boundWeights(champion, { technical: 90, fundamental: 1, sentiment: 1, momentum: 1, risk: 1 });
    expect(Object.values(bounded).reduce((sum, value) => sum + value, 0)).toBe(100);
    expect(bounded.fundamental).toBe(0);
    expect(bounded.sentiment).toBe(0);
    expect(bounded.technical).toBeGreaterThanOrEqual(32);
    expect(bounded.technical).toBeLessThanOrEqual(48);
  });

  it('keeps exact regime champion ownership separate from global fallback', async () => {
    const models = [
      { id: 'global', regime: 'all', status: 'champion' },
      { id: 'bear', regime: 'high_vol_bear', status: 'champion' },
    ];
    const sr = { entities: { StrategyModel: { filter: async () => models } } };
    await expect(getExactChampion(sr, 'user-1', 'high_vol_bear')).resolves.toMatchObject({ id: 'bear' });
    await expect(getExactChampion(sr, 'user-1', 'low_vol_bull')).resolves.toBeNull();
  });

  it('fails closed when a regime has multiple champions', async () => {
    const sr = { entities: { StrategyModel: { filter: async () => [
      { id: 'a', regime: 'all' }, { id: 'b', regime: 'all' },
    ] } } };
    await expect(getExactChampion(sr, 'user-1', 'all')).rejects.toThrow('MULTIPLE_CHAMPIONS_FOR_REGIME');
  });

  it('counts one logical TradeIntent once despite multiple decision fragments', async () => {
    const rows = Array.from({ length: 4 }, (_, index) => ({
      id: `fragment-${index}`, trade_intent_id: 'intent-1', lineage_complete: true,
      outcome_status: 'realized', realized_return: 0.01, technical_score: 60,
      momentum_score: 60, risk_score: 60, regime: 'transition',
    }));
    const sr = { entities: { AITradeDecision: { filter: async () => rows } } };
    await expect(collectOutcomes(sr, 'user-1', 'all')).resolves.toHaveLength(1);
  });

  it('forces automatic governance to manual approval for paper credentials', async () => {
    const global = { id: 'global', user_id: 'user-1', model_id: 'alpha', version: '1.0.0', regime: 'all', status: 'champion', weights: champion };
    const sr = { entities: {
      StrategyModel: { filter: async () => [global] },
      AITradeDecision: { filter: async () => [] },
      BrokerCredential: { filter: async () => [{ mode: 'paper', status: 'active' }] },
      User: { update: async () => {} },
    } };
    const result = await runGovernanceCycle(sr, 'user-1', { promotion_mode: 'automatic' });
    expect(result.mode).toBe('manual_approval');
    expect(result.scopes).toEqual(['all']);
  });
});
