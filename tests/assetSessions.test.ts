import { describe, expect, it } from 'vitest';
import { assetSessionDecision, canonicalAssetClass, isContinuousAssetClass } from '../base44/shared/assetSessions.ts';

describe('asset-aware trading sessions', () => {
  it('keeps crypto continuously eligible', () => {
    expect(isContinuousAssetClass('crypto')).toBe(true);
    expect(assetSessionDecision('crypto', new Date('2026-08-08T12:00:00Z'))).toMatchObject({ allowed: true, session: 'continuous' });
  });

  it('allows equities only during the US regular session', () => {
    expect(assetSessionDecision('stocks', new Date('2026-08-07T14:00:00Z')).allowed).toBe(true);
    expect(assetSessionDecision('equities', new Date('2026-08-07T21:00:00Z')).allowed).toBe(false);
  });

  it('fails closed for asset classes without an implemented session calendar', () => {
    expect(assetSessionDecision('forex').allowed).toBe(false);
    expect(canonicalAssetClass('stock')).toBe('equities');
  });
});
