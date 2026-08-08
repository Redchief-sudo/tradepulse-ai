const HEARTBEAT_FRESH_MS = 90_000;

export function scannerActivity({ tradingActive, sessionState, latestRun, pendingRequests = 0, now = Date.now() }) {
  const heartbeatAt = latestRun?.last_heartbeat_at || latestRun?.started_at;
  const heartbeatAge = heartbeatAt ? now - new Date(heartbeatAt).getTime() : Number.POSITIVE_INFINITY;
  if (latestRun?.status === 'running' && heartbeatAge >= 0 && heartbeatAge <= HEARTBEAT_FRESH_MS) {
    return { state: 'scanning', label: 'Actively scanning for new opportunities', detail: 'Backend scan heartbeat is live' };
  }
  if (latestRun?.status === 'running') {
    return { state: 'stale', label: 'Scan heartbeat is stale', detail: 'The current scan needs operational review' };
  }
  if (pendingRequests > 0) {
    return { state: 'queued', label: 'Opportunity scan queued', detail: 'Waiting for the scan coordinator' };
  }
  if (tradingActive) {
    return {
      state: 'monitoring',
      label: 'Opportunity monitoring is active',
      detail: sessionState === 'market_closed'
        ? 'Watching supported 24/7 assets; US-session assets are paused'
        : 'The next asset-aware scan runs on the 15-minute schedule',
    };
  }
  return { state: 'stopped', label: 'Opportunity scanner is stopped', detail: 'Start Trading to enable autonomous scans' };
}
