export function nextScanGeneration(runs: any[]) {
  return runs.reduce((max, run) => Math.max(max, Number(run.scan_generation) || 0), 0) + 1;
}

export function isScheduledScanDue(nowMs: number, runs: any[], cadenceMs = 15 * 60 * 1000) {
  const started = runs.map((run) => new Date(run.started_at || run.created_date).getTime()).filter(Number.isFinite);
  return started.length === 0 || nowMs - Math.max(...started) >= cadenceMs;
}

export function hasNewerScanGeneration(runs: any[], generation: number) {
  return runs.some((run) => Number(run.scan_generation) > generation);
}

export function isSuccessfulScanTerminal(run: any) {
  return Boolean(run && ['completed', 'no_candidates'].includes(run.status));
}
