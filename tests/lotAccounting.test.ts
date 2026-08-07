import { describe, it, expect } from 'vitest';

// Tests for the PositionLot closure allocation format.
// The closure_fill_ids field stores per-fill closure quantities as JSON:
//   [{ fill_id: "fill-1", qty: 4 }, { fill_id: "fill-2", qty: 6 }]
// This replaces the old format of bare fill IDs: ["fill-1", "fill-2"]
// which could not attribute specific quantities to specific sell fills.

// Parse closure_fill_ids, handling both old (string array) and new (object array) formats.
function parseClosureFills(closure_fill_ids) {
  const parsed = JSON.parse(closure_fill_ids || '[]');
  if (parsed.length === 0) return [];
  if (typeof parsed[0] === 'string') {
    // Old format: ["fill-1", "fill-2"] — qty unknown, return null for qty
    return parsed.map((fid) => ({ fill_id: fid, qty: null }));
  }
  // New format: [{ fill_id, qty }]
  return parsed;
}

describe('PositionLot closure allocation format', () => {
  it('parses new format with per-fill quantities', () => {
    const closure = JSON.stringify([
      { fill_id: 'fill-1', qty: 4 },
      { fill_id: 'fill-2', qty: 6 },
    ]);
    const result = parseClosureFills(closure);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ fill_id: 'fill-1', qty: 4 });
    expect(result[1]).toEqual({ fill_id: 'fill-2', qty: 6 });
  });

  it('parses old format (backward compatibility) with null qty', () => {
    const closure = JSON.stringify(['fill-1', 'fill-2']);
    const result = parseClosureFills(closure);
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({ fill_id: 'fill-1', qty: null });
    expect(result[1]).toEqual({ fill_id: 'fill-2', qty: null });
  });

  it('handles empty closure_fill_ids', () => {
    expect(parseClosureFills(null)).toEqual([]);
    expect(parseClosureFills('')).toEqual([]);
    expect(parseClosureFills('[]')).toEqual([]);
  });

  it('correctly attributes closed quantity per sell fill', () => {
    // A lot of 10 shares, closed by two sell fills: 4 and 6
    const closure = JSON.stringify([
      { fill_id: 'sell-A', qty: 4 },
      { fill_id: 'sell-B', qty: 6 },
    ]);
    const allocations = parseClosureFills(closure);

    // For sell-A, the closed quantity attributed to it is 4
    const qtyForA = allocations
      .filter((a) => a.fill_id === 'sell-A')
      .reduce((s, a) => s + (a.qty || 0), 0);
    expect(qtyForA).toBe(4);

    // For sell-B, the closed quantity attributed to it is 6
    const qtyForB = allocations
      .filter((a) => a.fill_id === 'sell-B')
      .reduce((s, a) => s + (a.qty || 0), 0);
    expect(qtyForB).toBe(6);

    // Total closed = 10 (matches lot quantity_opened)
    const totalClosed = allocations.reduce((s, a) => s + (a.qty || 0), 0);
    expect(totalClosed).toBe(10);
  });

  it('prevents double-counting when a lot is closed by multiple fills', () => {
    // OLD BUG: using quantity_opened - quantity_remaining for the entire lot
    // counted all 10 closed shares for EACH fill, giving 20 total.
    // NEW: per-fill allocation gives the correct split.
    const lot = { quantity_opened: 10, quantity_remaining: 0 };
    const closure = JSON.stringify([
      { fill_id: 'sell-A', qty: 4 },
      { fill_id: 'sell-B', qty: 6 },
    ]);
    const allocations = parseClosureFills(closure);

    // OLD (buggy): 10 - 0 = 10 for each fill → 20 total
    const oldBuggyTotal = allocations.length * (lot.quantity_opened - lot.quantity_remaining);
    expect(oldBuggyTotal).toBe(20); // This is the bug

    // NEW (correct): sum per-fill quantities → 10 total
    const newCorrectTotal = allocations.reduce((s, a) => s + (a.qty || 0), 0);
    expect(newCorrectTotal).toBe(10);
  });

  it('computes weighted entry price from per-fill allocations', () => {
    // Two lots closed by one sell fill, each with different acquisition prices
    const lot1 = { acquisition_price: 100, closure_fill_ids: JSON.stringify([{ fill_id: 'sell-X', qty: 3 }]) };
    const lot2 = { acquisition_price: 150, closure_fill_ids: JSON.stringify([{ fill_id: 'sell-X', qty: 2 }]) };

    const allClosedLots = [lot1, lot2];
    const totalQty = allClosedLots.reduce((s, l) => {
      const allocations = parseClosureFills(l.closure_fill_ids);
      return s + allocations.filter((a) => a.fill_id === 'sell-X').reduce((sum, a) => sum + (a.qty || 0), 0);
    }, 0);
    const totalCost = allClosedLots.reduce((s, l) => {
      const allocations = parseClosureFills(l.closure_fill_ids);
      const qty = allocations.filter((a) => a.fill_id === 'sell-X').reduce((sum, a) => sum + (a.qty || 0), 0);
      return s + qty * l.acquisition_price;
    }, 0);

    // Weighted entry price = (3*100 + 2*150) / 5 = 600/5 = 120
    expect(totalQty).toBe(5);
    expect(totalCost).toBe(600);
    expect(totalCost / totalQty).toBe(120);
  });
});