import { describe, expect, it } from 'vitest';
import { scannerActivity } from '../src/lib/scanActivity.js';

const now = Date.parse('2026-08-08T12:00:00Z');

describe('dashboard scanner activity', () => {
  it('shows active scanning only with a fresh persisted heartbeat', () => {
    expect(scannerActivity({ tradingActive: true, latestRun: { status: 'running', last_heartbeat_at: '2026-08-08T11:59:30Z' }, now }).state).toBe('scanning');
    expect(scannerActivity({ tradingActive: true, latestRun: { status: 'running', last_heartbeat_at: '2026-08-08T11:55:00Z' }, now }).state).toBe('stale');
  });

  it('shows queued work before a ScanRun exists', () => {
    expect(scannerActivity({ tradingActive: true, pendingRequests: 1, now }).state).toBe('queued');
  });

  it('distinguishes monitoring from a scan executing now', () => {
    const result = scannerActivity({ tradingActive: true, sessionState: 'market_closed', latestRun: { status: 'completed' }, now });
    expect(result).toMatchObject({ state: 'monitoring' });
    expect(result.detail).toContain('24/7');
  });

  it('shows stopped when autonomous trading is disabled', () => {
    expect(scannerActivity({ tradingActive: false, latestRun: { status: 'completed' }, now }).state).toBe('stopped');
  });
});
