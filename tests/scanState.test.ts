import { describe, expect, it } from 'vitest';
import { hasNewerScanGeneration, isSuccessfulScanTerminal, nextScanGeneration } from '../base44/shared/scanState.ts';

describe('scan generation invalidation', () => {
  it('allocates a monotonically increasing generation', () => {
    expect(nextScanGeneration([])).toBe(1);
    expect(nextScanGeneration([{ scan_generation: 2 }, { scan_generation: 7 }])).toBe(8);
  });

  it('invalidates an older result when a newer generation exists', () => {
    const runs = [{ scan_generation: 4 }, { scan_generation: 5 }];
    expect(hasNewerScanGeneration(runs, 4)).toBe(true);
    expect(hasNewerScanGeneration(runs, 5)).toBe(false);
  });

  it('accepts only persisted successful terminal scan states', () => {
    expect(isSuccessfulScanTerminal({ status: 'completed' })).toBe(true);
    expect(isSuccessfulScanTerminal({ status: 'no_candidates' })).toBe(true);
    expect(isSuccessfulScanTerminal({ status: 'running' })).toBe(false);
    expect(isSuccessfulScanTerminal({ status: 'failed' })).toBe(false);
  });
});
