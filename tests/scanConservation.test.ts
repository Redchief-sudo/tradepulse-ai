import { describe, expect, it } from 'vitest';
import { summarizeCandidateDispositions } from '../base44/shared/scanConservation.ts';

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
