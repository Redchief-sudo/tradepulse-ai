import { describe, expect, it } from 'vitest';
import { calculateDailyReturn, calculatePositionDayPnl, indexClosedLotsByFillId } from '../base44/shared/dailyPerformance.ts';

describe('daily performance integrity', () => {
  it('reconstructs previous value before calculating position day P&L', () => {
    expect(calculatePositionDayPnl([{ shares: 1, current_price: 110, avg_price: 80, day_change_percent: 10 }])).toBeCloseTo(10);
  });

  it('does not publish a daily return without a proven starting equity', () => {
    expect(calculateDailyReturn(null, 1100)).toBeNull();
    expect(calculateDailyReturn(1000, 1100)).toBeCloseTo(10);
  });

  it('indexes current object-based and legacy string-based lot closures', () => {
    const objectLot = { id: 'object', status: 'closed', closure_fill_ids: JSON.stringify([{ fill_id: 'fill-1', qty: 2 }]) };
    const legacyLot = { id: 'legacy', status: 'partially_closed', closure_fill_ids: JSON.stringify(['fill-2']) };
    const { index } = indexClosedLotsByFillId([objectLot, legacyLot]);
    expect(index['fill-1']).toEqual([objectLot]);
    expect(index['fill-2']).toEqual([legacyLot]);
  });

  it('fails closed on malformed lot allocation evidence', () => {
    expect(indexClosedLotsByFillId([{ id: 'bad-lot', status: 'closed', closure_fill_ids: 'invalid' }])).toEqual({
      index: {},
      malformedLotIds: ['bad-lot'],
    });
  });
});
