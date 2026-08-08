import { describe, expect, it } from 'vitest';
import { deriveDecisionOutcome, deriveLotOutcome } from '../base44/shared/outcomes.ts';

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

  it('aggregates partial entry fills into one logical decision outcome', () => {
    const logicalDecision = { ...decision, trade_intent_id: 'intent-1' };
    const entryFills = Array.from({ length: 4 }, (_, index) => ({
      fill_id: `buy-${index}`, trade_intent_id: 'intent-1', side: 'buy', filled_quantity: 2.5,
      filled_price: 100 + index, timestamp: `2026-01-01T10:0${index}:00Z`,
    }));
    const lots = entryFills.map((fill, index) => ({
      originating_fill_id: fill.fill_id, provenance_quality: 'verified', quantity_opened: 2.5,
      quantity_remaining: 0, acquisition_price: fill.filled_price, acquisition_timestamp: fill.timestamp,
      closure_fill_ids: JSON.stringify([{ fill_id: 'sell-all', qty: 2.5 }]),
    }));
    const sell = { fill_id: 'sell-all', side: 'sell', filled_quantity: 10, filled_price: 110, commission: 2, timestamp: '2026-01-02T10:00:00Z' };
    const outcome = deriveDecisionOutcome(logicalDecision, lots, [...entryFills, sell]);
    expect(outcome).toMatchObject({ outcome_status: 'realized', outcome_quantity: 10, lineage_complete: true });
    expect(outcome.entry_fill_ids).toHaveLength(4);
    expect(outcome.exit_fill_ids).toEqual(['sell-all']);
    // The shared $2 exit commission is allocated once across all four lots.
    expect(outcome.realized_return).toBeCloseTo((1100 - 2 - 1015) / 1015);
  });
});
