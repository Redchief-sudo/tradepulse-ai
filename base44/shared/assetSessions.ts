import { isUsMarketOpen, usMarketSession } from './marketHours.ts';

export function canonicalAssetClass(assetClass) {
  const value = String(assetClass || 'stocks').toLowerCase();
  if (['stock', 'stocks', 'equity', 'equities'].includes(value)) return 'equities';
  return value;
}

export function isContinuousAssetClass(assetClass) {
  return canonicalAssetClass(assetClass) === 'crypto';
}

export function assetSessionDecision(assetClass, now = new Date()) {
  const canonical = canonicalAssetClass(assetClass);
  if (canonical === 'crypto') return { allowed: true, session: 'continuous', assetClass: canonical };
  if (canonical === 'equities' && isUsMarketOpen(now)) {
    return { allowed: true, session: 'regular', assetClass: canonical };
  }
  return { allowed: false, session: canonical === 'equities' ? usMarketSession(now) : 'unsupported_session', assetClass: canonical };
}
