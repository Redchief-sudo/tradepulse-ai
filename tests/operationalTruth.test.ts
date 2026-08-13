import { describe, expect, it } from 'vitest';
import { brokerFillConservation, dailyReportStatus, healthStatus, isOpenBrokerOrder, reconciliationIsFresh, settlementHealthSeverity, snapshotConservation } from '../base44/shared/operationalTruth.ts';
import { providerDataFailure, providerHttpFailure, providerRequestFailure } from '../base44/shared/marketDataAdapter.ts';

describe('provider failure classification', () => {
  it.each([[429, 'rate_limited'], [400, 'provider_4xx'], [500, 'provider_5xx']])('retains HTTP %s cause', (code, status) => {
    expect(providerHttpFailure('test', 'ABC', 'quote', code, 'failed')).toMatchObject({ http_status: code, status, retryable: code === 429 || code >= 500 });
  });
  it('distinguishes timeout from network failure', () => {
    expect(providerRequestFailure('test', 'ABC', 'quote', new Error('request timeout')).status).toBe('timeout');
    expect(providerRequestFailure('test', 'ABC', 'quote', new Error('DNS failure')).status).toBe('network_failure');
  });
  it('normalizes non-HTTP provider failures', () => expect(providerDataFailure('test', 'ABC', 'quote', 'NO_DATA', 'missing')).toMatchObject({ status: 'invalid_data', received_at: expect.any(String), retryable: false }));
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
  const clean = { financialIntegrityBlocked: false, settlementIntegrityBlocked: 0, reconciliationStatus: 'clean', unaccountedBrokerFills: 0, missingLedgerFills: 0, extraLedgerFills: 0, positionDriftCount: 0, brokerDataUnavailable: false, cashDataUnavailable: false, settlementPending: 0, settlementFailed: 0, unexplainedCandidates: 0, healthCheckFailures: 0 };
  it('requires zero unresolved evidence for healthy', () => expect(dailyReportStatus(clean)).toBe('healthy'));
  it('marks drift as reconciliation required', () => expect(dailyReportStatus({ ...clean, positionDriftCount: 1 })).toBe('reconciliation_required'));
  it('marks unresolved settlement incomplete', () => expect(dailyReportStatus({ ...clean, settlementPending: 1 })).toBe('incomplete'));
  it('requires broker and ledger fills to conserve', () => {
    expect(brokerFillConservation([{ id: 'a1', order_id: 'o1', qty: '2' }], [{ fill_id: 'o1:fill:2', broker_order_id: 'o1', filled_quantity: 2 }])).toMatchObject({ ok: true, alpaca_fill_count: 1, ledger_fill_count: 1 });
    expect(dailyReportStatus({ ...clean, missingLedgerFills: 1 })).toBe('reconciliation_required');
  });
  it('requires reconciliation after both fill and market close', () => {
    expect(reconciliationIsFresh('2026-08-11T20:05:00Z', '2026-08-11T19:00:00Z', '2026-08-11T20:00:00Z')).toBe(true);
    expect(reconciliationIsFresh('2026-08-11T19:30:00Z', '2026-08-11T19:00:00Z', '2026-08-11T20:00:00Z')).toBe(false);
  });
  it('recognizes partially-filled live remainders and critical settlement states', () => {
    expect(isOpenBrokerOrder({ status: 'partially_filled', broker_order_id: 'o1', requested_quantity: 100, filled_quantity: 40 })).toBe(true);
    expect(settlementHealthSeverity({ status: 'integrity_blocked' })).toBe('critical');
    expect(settlementHealthSeverity({ status: 'terminal_failed' })).toBe('critical');
  });
});
