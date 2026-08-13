import { describe, expect, it } from 'vitest';
import { dailyReportStatus, healthStatus, snapshotConservation } from '../base44/shared/operationalTruth.ts';
import { providerHttpFailure, providerRequestFailure } from '../base44/shared/marketDataAdapter.ts';

describe('provider failure classification', () => {
  it.each([[429, 'rate_limited'], [400, 'provider_4xx'], [500, 'provider_5xx']])('retains HTTP %s cause', (code, status) => {
    expect(providerHttpFailure('test', 'ABC', 'quote', code, 'failed')).toMatchObject({ http_status: code, status, retryable: code === 429 || code >= 500 });
  });
  it('distinguishes timeout from network failure', () => {
    expect(providerRequestFailure('test', 'ABC', 'quote', new Error('request timeout')).status).toBe('timeout');
    expect(providerRequestFailure('test', 'ABC', 'quote', new Error('DNS failure')).status).toBe('network_failure');
  });
});

describe('snapshot and health truth', () => {
  it('enforces requested conservation and fails zero capture', () => {
    expect(snapshotConservation(2, 0, 2, 0)).toMatchObject({ accounted: 2, ok: false });
  });
  it('reports health unknown when required inspection fails', () => {
    expect(healthStatus([], [{ check: 'broker', message: 'failed' }])).toBe('health_unknown');
  });
});

describe('daily report certificate', () => {
  const clean = { financialIntegrityBlocked: false, settlementIntegrityBlocked: 0, reconciliationStatus: 'clean', unaccountedBrokerFills: 0, positionDriftCount: 0, brokerDataUnavailable: false, cashDataUnavailable: false, settlementPending: 0, settlementFailed: 0, unexplainedCandidates: 0, healthCheckFailures: 0 };
  it('requires zero unresolved evidence for healthy', () => expect(dailyReportStatus(clean)).toBe('healthy'));
  it('marks drift as reconciliation required', () => expect(dailyReportStatus({ ...clean, positionDriftCount: 1 })).toBe('reconciliation_required'));
  it('marks unresolved settlement incomplete', () => expect(dailyReportStatus({ ...clean, settlementPending: 1 })).toBe('incomplete'));
});
