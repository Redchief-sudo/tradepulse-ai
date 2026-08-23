import { describe, it, expect } from 'vitest';
import { classifyBrokerSubmitError, attemptCashReservation } from '../base44/shared/execution.ts';
import { AlpacaError } from '../base44/shared/alpacaErrors.ts';

// Mock factory matching the pattern in tests/cashLedger.test.ts.
function createMockSr(initialEntries = []) {
  const store = [...initialEntries];
  let idCounter = 0;
  const nextId = () => `entry-${++idCounter}`;

  const filter = async (query) => {
    return store.filter((e) => {
      for (const [key, val] of Object.entries(query)) {
        if (key === 'user_id' && e.user_id !== val) return false;
        if (key === 'entry_type' && e.entry_type !== val) return false;
        if (key === 'trade_intent_id' && e.trade_intent_id !== val) return false;
      }
      return true;
    });
  };

  const create = async (data) => {
    const entry = { ...data, id: nextId(), created_date: new Date(Date.now() + idCounter).toISOString() };
    store.push(entry);
    return entry;
  };

  const del = async (id) => {
    const idx = store.findIndex((e) => e.id === id);
    if (idx >= 0) store.splice(idx, 1);
  };

  return { entities: { CashEntry: { filter, create, delete: del } }, _store: store };
}

const userId = 'test-user';

describe('classifyBrokerSubmitError (H8 — fixes AlpacaError.isInsufficientBuyingPower() having zero call sites)', () => {
  it('classifies a 403 insufficient-buying-power AlpacaError distinctly', () => {
    const e = new AlpacaError('account does not have sufficient buying power for this order', 403, 'req-1', 'insufficient_buying_power');
    expect(classifyBrokerSubmitError(e)).toBe('BROKER_INSUFFICIENT_BUYING_POWER');
  });

  it('falls back to the generic reason for other AlpacaErrors', () => {
    const e = new AlpacaError('order rejected: symbol not tradable', 422, 'req-2', 'invalid_symbol');
    expect(classifyBrokerSubmitError(e)).toBe('BROKER_SUBMIT_ERROR: order rejected: symbol not tradable');
  });

  it('falls back to the generic reason for a non-Alpaca error', () => {
    const e = new Error('network timeout');
    expect(classifyBrokerSubmitError(e)).toBe('BROKER_SUBMIT_ERROR: network timeout');
  });
});

describe('attemptCashReservation (BLOCKER-3 — fixes reserveCash() having zero call sites in execution.ts)', () => {
  it('rejects cleanly when available cash is insufficient', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 500, balance_after: 500, created_date: '2025-01-01T10:00:00Z' },
    ]);

    const result = await attemptCashReservation(sr, userId, {
      symbol: 'AAPL', tradeIntentId: 'intent-1', portfolioId: null, amount: 1000,
    });

    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/INSUFFICIENT_CASH_FOR_RESERVATION/);
    // No reservation entry should have been left behind on rejection.
    const reservations = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'reservation' });
    expect(reservations.length).toBe(0);
  });

  it('succeeds and records a reservation entry when cash is sufficient', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 10000, balance_after: 10000, created_date: '2025-01-01T10:00:00Z' },
    ]);

    const result = await attemptCashReservation(sr, userId, {
      symbol: 'AAPL', tradeIntentId: 'intent-2', portfolioId: null, amount: 1500,
    });

    expect(result.ok).toBe(true);
    const reservations = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-2' });
    expect(reservations.length).toBe(1);
    expect(reservations[0].amount).toBe(-1500);
  });

  it('is idempotent — a retried reservation for the same trade_intent_id does not double-reserve', async () => {
    const sr = createMockSr([
      { id: 'opening', user_id: userId, entry_type: 'account_opening', amount: 10000, balance_after: 10000, created_date: '2025-01-01T10:00:00Z' },
    ]);

    await attemptCashReservation(sr, userId, { symbol: 'AAPL', tradeIntentId: 'intent-3', portfolioId: null, amount: 1000 });
    const second = await attemptCashReservation(sr, userId, { symbol: 'AAPL', tradeIntentId: 'intent-3', portfolioId: null, amount: 1000 });

    expect(second.ok).toBe(true);
    const reservations = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'reservation', trade_intent_id: 'intent-3' });
    expect(reservations.length).toBe(1);
  });
});
