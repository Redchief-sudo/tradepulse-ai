import { describe, expect, it } from 'vitest';
import { deriveLotOutcome } from '../base44/shared/outcomes.ts';

const decision = { settlement_event_id: 'buy-fill-1', price: 100, created_date: '2026-01-01T10:00:00Z' };

describe('outcome lineage', () => {
  it('allocates multiple partial exits through exact lot closure fill ids', () => {
    const lots = [{
      originating_fill_id: 'buy-fill-1', provenance_quality: 'verified', quantity_opened: 10,
      quantity_remaining: 0, acquisition_price: 100, acquisition_timestamp: '2026-01-01T10:00:00Z',
      closure_fill_ids: JSON.stringify([{ fill_id: 'sell-a', qty: 4 }, { fill_id: 'sell-b', qty: 6 }]),
    }];
    const fills = [
      { fill_id: 'buy-fill-1', filled_price: 100, timestamp: '2026-01-01T10:00:00Z' },
      { fill_id: 'sell-a', filled_price: 110, timestamp: '2026-01-01T11:00:00Z' },
      { fill_id: 'sell-b', filled_price: 90, timestamp: '2026-01-01T12:00:00Z' },
    ];
    const outcome = deriveLotOutcome(decision, lots, fills);
    expect(outcome).toMatchObject({ lineage_complete: true, outcome_status: 'realized', outcome_quantity: 10, entry_fill_id: 'buy-fill-1' });
    expect(outcome.realized_return).toBeCloseTo(-0.02);
    expect(outcome.exit_fill_ids).toEqual(['sell-a', 'sell-b']);
  });

  it('does not call a partially closed lot realized', () => {
    const lots = [{ originating_fill_id: 'buy-fill-1', provenance_quality: 'verified', quantity_opened: 10, quantity_remaining: 6, acquisition_price: 100, closure_fill_ids: '[{"fill_id":"sell-a","qty":4}]' }];
    const outcome = deriveLotOutcome(decision, lots, [{ fill_id: 'buy-fill-1', filled_price: 100, timestamp: '2026-01-01T10:00:00Z' }, { fill_id: 'sell-a', filled_price: 110, timestamp: '2026-01-01T11:00:00Z' }]);
    expect(outcome).toMatchObject({ lineage_complete: true, outcome_status: 'open', outcome_quantity: 4 });
  });

  it('fails closed when lot or closure-fill lineage is missing', () => {
    expect(deriveLotOutcome(decision, [], [])).toMatchObject({ lineage_complete: false, outcome_status: 'open' });
    const lots = [{ originating_fill_id: 'buy-fill-1', provenance_quality: 'verified', quantity_opened: 1, quantity_remaining: 0, acquisition_price: 100, closure_fill_ids: '[{"fill_id":"missing","qty":1}]' }];
    expect(deriveLotOutcome(decision, lots, [])).toMatchObject({ lineage_complete: false, outcome_status: 'open' });
  });
});
