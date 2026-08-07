import { describe, it, expect, vi } from 'vitest';

// Mock-based tests for the cash ledger's create-then-dedup pattern.
// These verify the idempotency logic without a real database.

// Mock factory: creates a fake sr.entities.CashEntry with in-memory storage
function createMockSr(initialEntries = []) {
  const store = [...initialEntries];
  let idCounter = 0;
  const nextId = () => `entry-${++idCounter}`;

  const filter = async (query) => {
    return store.filter((e) => {
      for (const [key, val] of Object.entries(query)) {
        if (key === 'user_id' && e.user_id !== val) return false;
        if (key === 'entry_type' && e.entry_type !== val) return false;
        if (key === 'fill_id' && e.fill_id !== val) return false;
        if (key === 'trade_intent_id' && e.trade_intent_id !== val) return false;
        if (key === 'description' && e.description !== val) return false;
      }
      return true;
    });
  };

  const create = async (data) => {
    const entry = {
      ...data,
      id: nextId(),
      created_date: new Date(Date.now() + idCounter).toISOString(), // stagger timestamps
    };
    store.push(entry);
    return entry;
  };

  const del = async (id) => {
    const idx = store.findIndex((e) => e.id === id);
    if (idx >= 0) store.splice(idx, 1);
  };

  return {
    entities: {
      CashEntry: { filter, create, delete: del },
    },
    _store: store,
  };
}

const userId = 'test-user';

describe('cashLedger — create-then-dedup idempotency', () => {
  it('initializePaperAccount: deduplicates opening entries from a race', async () => {
    // Simulate a race: two opening entries already exist
    const sr = createMockSr([
      { id: 'old-1', user_id: userId, entry_type: 'account_opening', amount: 1000, balance_after: 1000, description: 'Account opening balance', created_date: '2025-01-01T10:00:00Z' },
      { id: 'old-2', user_id: userId, entry_type: 'account_opening', amount: 1000, balance_after: 1000, description: 'Account opening balance', created_date: '2025-01-01T10:00:01Z' },
    ]);

    // Import after mock is set up
    const { initializePaperAccount } = await import('../base44/shared/cashLedger.ts');
    const result = await initializePaperAccount(sr, userId);

    expect(result).toBe(1000);
    const remaining = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'account_opening' });
    expect(remaining.length).toBe(1);
    expect(remaining[0].id).toBe('old-1'); // earliest wins
  });

  it('initializePaperAccount: does not create a duplicate when one already exists', async () => {
    const sr = createMockSr([
      { id: 'existing-1', user_id: userId, entry_type: 'account_opening', amount: 5000, balance_after: 5000, description: 'Account opening balance', created_date: '2025-01-01T10:00:00Z' },
    ]);

    const { initializePaperAccount } = await import('../base44/shared/cashLedger.ts');
    const result = await initializePaperAccount(sr, userId);

    expect(result).toBe(5000);
    expect(sr._store.length).toBe(1); // no new entry created
  });

  it('initializePaperAccount: creates opening entry when none exists', async () => {
    const sr = createMockSr([]);

    const { initializePaperAccount } = await import('../base44/shared/cashLedger.ts');
    const result = await initializePaperAccount(sr, userId);

    expect(result).toBe(1000);
    const entries = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'account_opening' });
    expect(entries.length).toBe(1);
    expect(entries[0].amount).toBe(1000);
  });

  it('recordCashEntry: deduplicates by fill_id + entry_type', async () => {
    // Simulate a race: two buy entries for the same fill already exist
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 10000, balance_after: 10000, description: 'Account opening balance', created_date: '2025-01-01T10:00:00Z' },
      { id: 'buy-1', user_id: userId, entry_type: 'buy', fill_id: 'fill-123', amount: -500, balance_after: 9500, created_date: '2025-01-01T11:00:00Z' },
      { id: 'buy-2', user_id: userId, entry_type: 'buy', fill_id: 'fill-123', amount: -500, balance_after: 9500, created_date: '2025-01-01T11:00:01Z' },
    ]);

    const { recordCashEntry } = await import('../base44/shared/cashLedger.ts');

    // Try to record the same fill again — should be a no-op
    const result = await recordCashEntry(sr, userId, {
      entry_type: 'buy',
      amount: -500,
      fill_id: 'fill-123',
      symbol: 'AAPL',
    });

    // Should not create a third buy entry
    const buyEntries = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'buy', fill_id: 'fill-123' });
    expect(buyEntries.length).toBe(1);
    expect(buyEntries[0].id).toBe('buy-1'); // earliest wins
  });

  it('reserveCash: deduplicates by trade_intent_id', async () => {
    // Simulate a race: two reservations for the same intent
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 10000, balance_after: 10000, description: 'Account opening balance', created_date: '2025-01-01T10:00:00Z' },
      { id: 'res-1', user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-1', amount: -300, balance_after: 9700, created_date: '2025-01-01T11:00:00Z' },
      { id: 'res-2', user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-1', amount: -300, balance_after: 9700, created_date: '2025-01-01T11:00:01Z' },
    ]);

    const { reserveCash } = await import('../base44/shared/cashLedger.ts');

    // Try to reserve again — should be a no-op
    await reserveCash(sr, userId, {
      symbol: 'AAPL',
      amount: 300,
      trade_intent_id: 'intent-1',
    });

    const reservations = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-1' });
    expect(reservations.length).toBe(1);
    expect(reservations[0].id).toBe('res-1'); // earliest wins
  });

  it('reserveCash: throws when insufficient available cash', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 500, balance_after: 500, description: 'Account opening balance', created_date: '2025-01-01T10:00:00Z' },
    ]);

    const { reserveCash } = await import('../base44/shared/cashLedger.ts');

    await expect(
      reserveCash(sr, userId, { symbol: 'AAPL', amount: 1000, trade_intent_id: 'intent-x' })
    ).rejects.toThrow('INSUFFICIENT_CASH_FOR_RESERVATION');
  });

  it('getCashBalance: sums all entry amounts correctly', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 5000, balance_after: 5000, created_date: '2025-01-01T10:00:00Z' },
      { id: 'buy-1', user_id: userId, entry_type: 'buy', amount: -1000, balance_after: 4000, created_date: '2025-01-01T11:00:00Z' },
      { id: 'sell-1', user_id: userId, entry_type: 'sell', amount: 600, balance_after: 4600, created_date: '2025-01-01T12:00:00Z' },
    ]);

    const { getCashBalance } = await import('../base44/shared/cashLedger.ts');
    const balance = await getCashBalance(sr, userId);
    expect(balance).toBe(4600); // 5000 - 1000 + 600
  });

  it('getAvailableCash: accounts for open reservations', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 5000, balance_after: 5000, created_date: '2025-01-01T10:00:00Z' },
      { id: 'res-1', user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-1', amount: -800, balance_after: 4200, created_date: '2025-01-01T11:00:00Z' },
    ]);

    const { getAvailableCash } = await import('../base44/shared/cashLedger.ts');
    const { available, balance, openReservations } = await getAvailableCash(sr, userId);

    expect(balance).toBe(4200); // 5000 - 800
    expect(available).toBe(4200); // reservations already in balance — no double-subtraction
    expect(openReservations).toBe(800);
  });

  it('getPaperEquity: adds back open reservations to balance', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 5000, balance_after: 5000, created_date: '2025-01-01T10:00:00Z' },
      { id: 'res-1', user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-1', amount: -800, balance_after: 4200, created_date: '2025-01-01T11:00:00Z' },
    ]);

    const { getPaperEquity } = await import('../base44/shared/cashLedger.ts');
    const equity = await getPaperEquity(sr, userId, 3000); // 3000 in holdings

    // equity = balance(4200) + openReservations(800) + holdingsValue(3000) = 8000
    expect(equity).toBe(8000);
  });
});