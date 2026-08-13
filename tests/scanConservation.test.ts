import { describe, expect, it } from 'vitest';
import { summarizeCandidateDispositions, summarizeFillSettlement } from '../base44/shared/scanConservation.ts';

describe('scan candidate conservation', () => {
  it('requires one disposition for every executable candidate', () => {
    const candidates = [{ symbol: 'MSFT' }, { symbol: 'NVDA' }, { symbol: 'JPM' }];
    const dispositions = new Map([
      ['MSFT', 'filtered:technical_score_below_55'],
      ['NVDA', 'vetoed:committee_no_consensus'],
      ['JPM', 'filled:completed'],
    ]);
    expect(summarizeCandidateDispositions(candidates, dispositions)).toMatchObject({ ok: true, total: 3, accounted: 3, missing: [] });
  });

  it('fails when a candidate disappears between stages', () => {
    const result = summarizeCandidateDispositions([{ symbol: 'MSFT' }, { symbol: 'NVDA' }], new Map([['MSFT', 'filtered:rsi_overbought']]));
    expect(result.ok).toBe(false);
    expect(result.missing).toEqual(['NVDA']);
  });
});

describe('scan fill settlement conservation', () => {
  it('does not complete when an accepted fill settlement is pending', () => {
    const result = summarizeFillSettlement([{ fill_id: 'f1' }], [{ event_id: 'f1', status: 'pending' }]);
    expect(result).toMatchObject({ ok: false, accepted_fills: 1, settlement_pending: 1 });
  });
  it('completes only with integrity-verified settlement', () => {
    const result = summarizeFillSettlement([{ fill_id: 'f1' }], [{ event_id: 'f1', status: 'completed', integrity_verified: true }]);
    expect(result).toMatchObject({ ok: true, settlement_completed: 1 });
  });
});
