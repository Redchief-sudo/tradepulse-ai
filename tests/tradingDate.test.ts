import { describe, expect, it } from 'vitest';
import { newYorkDateString } from '../src/lib/tradingDate.js';

describe('New York trading date', () => {
  it('does not use the next UTC date before New York midnight', () => {
    expect(newYorkDateString(new Date('2026-08-12T02:30:00.000Z'))).toBe('2026-08-11');
  });

  it('advances after New York midnight', () => {
    expect(newYorkDateString(new Date('2026-08-12T04:30:00.000Z'))).toBe('2026-08-12');
  });
});
