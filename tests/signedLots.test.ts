import { describe, expect, it } from 'vitest';
import { planSignedLotFill, signedLotQuantity } from '../base44/shared/signedLots.ts';

const longLot = (quantity = 13, price = 500) => ({
  id: 'long-1', position_side: 'long', quantity_opened: quantity, quantity_remaining: quantity,
  acquisition_price: price, acquisition_timestamp: '2026-08-10T10:00:00Z', status: 'open', closure_fill_ids: '[]',
});

const shortLot = (quantity = 13, price = 500) => ({
  id: 'short-1', position_side: 'short', quantity_opened: quantity, quantity_remaining: quantity,
  acquisition_price: price, acquisition_timestamp: '2026-08-10T10:00:00Z', status: 'open', closure_fill_ids: '[]',
});

describe('signed lot settlement planning', () => {
  it('closes a long and opens the short residual when a sell crosses zero', () => {
    const plan = planSignedLotFill([longLot()], { event_id: 'sell-26', side: 'sell', quantity: 26, price: 510 });
    expect(plan.closures).toHaveLength(1);
    expect(plan.closures[0].quantity).toBe(13);
    expect(plan.openingDirection).toBe('short');
    expect(plan.openingQuantity).toBe(13);
    expect(plan.realizedPnl).toBe(130);
  });

  it('covers a short and opens the long residual when a buy crosses zero', () => {
    const plan = planSignedLotFill([shortLot()], { event_id: 'buy-20', side: 'buy', quantity: 20, price: 480 });
    expect(plan.closures[0].quantity).toBe(13);
    expect(plan.openingDirection).toBe('long');
    expect(plan.openingQuantity).toBe(7);
    expect(plan.realizedPnl).toBe(260);
  });

  it('is replay-safe after the close and residual opening were persisted', () => {
    const closed = { ...longLot(), quantity_remaining: 0, status: 'closed', closure_fill_ids: '[{"fill_id":"sell-26","qty":13}]' };
    const residual = { ...shortLot(), id: 'short-residual', originating_fill_id: 'sell-26' };
    const plan = planSignedLotFill([closed, residual], { event_id: 'sell-26', side: 'sell', quantity: 26, price: 510 });
    expect(plan.closures).toEqual([]);
    expect(plan.openingQuantity).toBe(0);
    expect(plan.realizedPnl).toBe(130);
  });

  it('treats legacy lots without position_side as long', () => {
    expect(signedLotQuantity({ quantity_remaining: 4 })).toBe(4);
    expect(signedLotQuantity({ position_side: 'short', quantity_remaining: 4 })).toBe(-4);
  });
});
