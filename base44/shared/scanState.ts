export function nextScanGeneration(runs: any[]) {
  return runs.reduce((max, run) => Math.max(max, Number(run.scan_generation) || 0), 0) + 1;
}

export function hasNewerScanGeneration(runs: any[], generation: number) {
  return runs.some((run) => Number(run.scan_generation) > generation);
}
