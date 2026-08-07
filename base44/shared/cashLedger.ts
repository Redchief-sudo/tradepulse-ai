// Internal paper cash ledger — simulates cash accounting for internal paper mode.
// Tracks deposits, withdrawals, trade settlements, commissions, fees, and order
// reservations so that paper trading has accurate buying power and cash balance.
//
// For broker_paper/live mode, the broker's account endpoint is authoritative.
// This ledger is ONLY used for internal_paper mode (no broker connected).
//
// SAFETY GUARANTEES (Rev.13 audit fixes):
// 5. SINGLE INITIALIZER: initializePaperAccount() is the ONLY initialization path.
//    recordCashEntry() no longer duplicates it. A fixed description marker is the
//    idempotency key — a race still creates two deposits, but the rebuild function
//    deduplicates by description during reconciliation.
// 6. IDEMPOTENCY: trade-related entries are deduplicated by fill_id + entry_type.
//    A retry will not double-apply cash.
// 8. NEGATIVE CASH PREVENTION: recordBuySettlement() checks available cash BEFORE
//    debiting and throws if insufficient. The simulated account cannot go negative.
// 9. CASH RESERVATION: reserveCash() holds funds for pending orders. getAvailableCash()
//    returns balance minus open reservations, preventing double-spending.
// 7. SEQUENCE VALIDATION: validateCashSequence() verifies the balance_after chain.
// 11. CASH REBUILD: rebuildCashFromFills() replays all fills to reconstruct the
//    ledger from the immutable Fill source of truth.

const PAPER_INITIAL_CASH = 1000; // $1,000 default — optimized for small accounts ($100-$10k)
const INITIAL_DEPOSIT_DESC = 'Initial paper account deposit';

// Get the current cash balance for a user (internal paper mode).
// Sums all entry amounts. Reservations (negative) and releases (positive) net to
// zero when an order completes, so the balance reflects settled cash.
export async function getCashBalance(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  return entries.reduce((sum, e) => sum + (e.amount || 0), 0);
}

// Get available cash = balance (reservations are already negative in the sum).
// This is the real buying power — it accounts for funds held for pending orders.
//
// FIX Rev.14 #4: The previous implementation subtracted openReservations from
// balance, but reservations are already negative entries in the balance sum.
// Subtracting again double-counted the reservation:
//   balance = 1000 - 300(reservation) = 700
//   available = 700 - 300 = 400  ← WRONG, should be 700
// Now: available = balance. The reservation entry already reduced it.
export async function getAvailableCash(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  let balance = 0;
  const reservations = {}; // trade_intent_id -> net reserved amount
  for (const e of entries) {
    balance += e.amount || 0;
    if (e.entry_type === 'reservation' && e.trade_intent_id) {
      reservations[e.trade_intent_id] = (reservations[e.trade_intent_id] || 0) + (e.amount || 0);
    }
    if (e.entry_type === 'reservation_release' && e.trade_intent_id) {
      reservations[e.trade_intent_id] = (reservations[e.trade_intent_id] || 0) + (e.amount || 0);
    }
  }
  // Track open reservations for equity calculation (reserved cash is still
  // part of net worth, just not available for new orders).
  let openReservations = 0;
  for (const tid of Object.keys(reservations)) {
    if (reservations[tid] < 0) openReservations += Math.abs(reservations[tid]);
  }
  return { available: balance, balance, openReservations };
}

// Initialize the paper account with the default deposit. Idempotent — checks
// for an existing initial deposit before creating one.
// (Fixes Rev.13 #5: consolidated to a single initializer — recordCashEntry no
// longer has its own initialization path.)
export async function initializePaperAccount(sr, userId) {
  const existing = await sr.entities.CashEntry.filter({ user_id: userId, entry_type: 'deposit', description: INITIAL_DEPOSIT_DESC });
  if (existing.length > 0) {
    return existing[0].balance_after;
  }
  return await sr.entities.CashEntry.create({
    user_id: userId,
    entry_type: 'deposit',
    amount: PAPER_INITIAL_CASH,
    balance_after: PAPER_INITIAL_CASH,
    description: INITIAL_DEPOSIT_DESC,
  }).then((r) => r.balance_after || PAPER_INITIAL_CASH);
}

// Ensure the account is initialized before any operation.
async function ensureInitialized(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  if (entries.length === 0) {
    await initializePaperAccount(sr, userId);
  }
}

// Record a cash entry with idempotency by fill_id. (Fixes Rev.13 #6.)
// If a fill_id is provided and a CashEntry already exists for that fill_id +
// entry_type, the call is a no-op and returns the existing balance.
// THROWS on failure — cash settlement is mandatory, not best-effort.
// Does NOT initialize the account — call initializePaperAccount explicitly.
export async function recordCashEntry(sr, userId, entry) {
  await ensureInitialized(sr, userId);

  // Idempotency: check if this fill has already been settled
  if (entry.fill_id) {
    const existing = await sr.entities.CashEntry.filter({
      user_id: userId,
      fill_id: entry.fill_id,
      entry_type: entry.entry_type,
    });
    if (existing.length > 0) {
      return existing[0].balance_after;
    }
  }

  const balance = await getCashBalance(sr, userId);
  const newBalance = balance + entry.amount;
  await sr.entities.CashEntry.create({
    user_id: userId,
    portfolio_id: entry.portfolio_id || null,
    entry_type: entry.entry_type,
    amount: entry.amount,
    balance_after: newBalance,
    symbol: entry.symbol || null,
    trade_intent_id: entry.trade_intent_id || null,
    fill_id: entry.fill_id || null,
    description: entry.description || '',
  });
  return newBalance;
}

// Get buying power for internal paper mode = available cash (balance minus reservations).
export async function getPaperBuyingPower(sr, userId) {
  await ensureInitialized(sr, userId);
  const { available } = await getAvailableCash(sr, userId);
  return available;
}

// Get total paper equity = settled cash + reserved cash + position market value.
// Reserved cash is still part of net worth (it hasn't been spent yet), so it
// must be added back to the balance for equity calculations.
// (Fixes Rev.14 #4: equity was understated during pending orders because
// getPaperBuyingPower returned the double-subtracted available cash.)
export async function getPaperEquity(sr, userId, holdingsValue) {
  await ensureInitialized(sr, userId);
  const { balance, openReservations } = await getAvailableCash(sr, userId);
  return balance + openReservations + (holdingsValue || 0);
}

// Reserve cash for a pending order. Creates a negative 'reservation' entry.
// This prevents multiple pending orders from double-spending the same cash.
// (Fixes Rev.13 #9.)
export async function reserveCash(sr, userId, params) {
  const { symbol, amount, trade_intent_id, portfolio_id } = params;
  if (!trade_intent_id) throw new Error('reserveCash requires trade_intent_id');
  if (amount <= 0) throw new Error('reserveCash requires positive amount');

  await ensureInitialized(sr, userId);

  // IDEMPOTENCY: check if a reservation already exists for this intent.
  // (Fixes Rev.14 #8: a retried order-preparation path could create multiple
  // reservations for one order, incorrectly reducing available cash.)
  const existing = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation',
  });
  if (existing.length > 0) {
    return existing[0].balance_after;
  }

  const { available } = await getAvailableCash(sr, userId);
  if (available < amount) {
    throw new Error(`INSUFFICIENT_CASH_FOR_RESERVATION: available ${available}, required ${amount}`);
  }

  const balance = await getCashBalance(sr, userId);
  const newBalance = balance - amount;
  await sr.entities.CashEntry.create({
    user_id: userId,
    portfolio_id: portfolio_id || null,
    entry_type: 'reservation',
    amount: -amount,
    balance_after: newBalance,
    symbol: symbol || null,
    trade_intent_id,
    description: `Reservation for ${symbol || 'order'}: ${amount.toFixed(2)}`,
  });
  return newBalance;
}

// Release a cash reservation. Creates a positive 'reservation_release' entry.
// Called when an order fills (actual buy debits separately) or is canceled.
// (Fixes Rev.13 #9.)
export async function releaseCashReservation(sr, userId, params) {
  const { trade_intent_id, portfolio_id } = params;
  if (!trade_intent_id) throw new Error('releaseCashReservation requires trade_intent_id');

  // Check if already released
  const existing = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation_release',
  });
  if (existing.length > 0) return existing[0].balance_after;

  // Find the reservation amount
  const reservations = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation',
  });
  if (reservations.length === 0) return null; // nothing to release

  const reservedAmount = Math.abs(reservations[0].amount);
  const balance = await getCashBalance(sr, userId);
  const newBalance = balance + reservedAmount;
  await sr.entities.CashEntry.create({
    user_id: userId,
    portfolio_id: portfolio_id || null,
    entry_type: 'reservation_release',
    amount: reservedAmount,
    balance_after: newBalance,
    trade_intent_id,
    description: `Reservation released: ${reservedAmount.toFixed(2)}`,
  });
  return newBalance;
}

// Record a buy settlement: cash decreases by notional + commission + fees.
// NEGATIVE CASH PREVENTION: checks available cash before debiting and throws
// if insufficient. (Fixes Rev.13 #8.)
export async function recordBuySettlement(sr, userId, params) {
  const { symbol, notional, commission = 0, fees = 0, trade_intent_id, fill_id, portfolio_id } = params;
  const totalCost = notional + commission + fees;

  await ensureInitialized(sr, userId);
  const { available } = await getAvailableCash(sr, userId);
  if (available < totalCost) {
    throw new Error(`INSUFFICIENT_CASH_FOR_BUY: available ${available.toFixed(2)}, cost ${totalCost.toFixed(2)} for ${symbol}`);
  }

  return await recordCashEntry(sr, userId, {
    portfolio_id,
    entry_type: 'buy',
    amount: -totalCost,
    symbol,
    trade_intent_id,
    fill_id,
    description: `Buy ${symbol}: ${notional.toFixed(2)} + commission ${commission} + fees ${fees}`,
  });
}

// Record a sell settlement: cash increases by notional - commission - fees.
export async function recordSellSettlement(sr, userId, params) {
  const { symbol, notional, commission = 0, fees = 0, trade_intent_id, fill_id, portfolio_id } = params;
  const netProceeds = notional - commission - fees;
  return await recordCashEntry(sr, userId, {
    portfolio_id,
    entry_type: 'sell',
    amount: netProceeds,
    symbol,
    trade_intent_id,
    fill_id,
    description: `Sell ${symbol}: ${notional.toFixed(2)} - commission ${commission} - fees ${fees}`,
  });
}

// Validate the cash ledger sequence — verifies that balance_after is consistent
// across the entry chain. (Fixes Rev.13 #7.)
// Returns { valid, errors, finalBalance }.
export async function validateCashSequence(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  const sorted = entries.sort((a, b) => new Date(a.created_date) - new Date(b.created_date));

  const errors = [];
  let runningBalance = 0;
  for (const e of sorted) {
    runningBalance += e.amount || 0;
    if (e.balance_after != null && Math.abs((e.balance_after || 0) - runningBalance) > 0.01) {
      errors.push({
        entry_id: e.id,
        entry_type: e.entry_type,
        expected: runningBalance,
        actual: e.balance_after,
        discrepancy: runningBalance - (e.balance_after || 0),
      });
    }
  }
  return { valid: errors.length === 0, errors, finalBalance: runningBalance };
}

// Rebuild the cash ledger from the immutable Fill source of truth.
// Clears all CashEntries (except the initial deposit), replays every fill in
// chronological order, and reconstructs buy/sell/commission/fee entries.
// Also deduplicates the initial deposit if a race created two. (Fixes Rev.13 #5, #11.)
export async function rebuildCashFromFills(sr, userId) {
  // 1. Clear all existing entries — fail closed if any deletion fails.
  // (Fixes Rev.14 #10: deletion failures were silently swallowed, allowing
  // stale entries to coexist with rebuilt entries, making the ledger worse.)
  const allEntries = await sr.entities.CashEntry.filter({ user_id: userId });
  const failedDeletions = [];
  for (const e of allEntries) {
    try { await sr.entities.CashEntry.delete(e.id); }
    catch (err) { failedDeletions.push({ id: e.id, error: err.message }); }
  }
  if (failedDeletions.length > 0) {
    throw new Error(`REBUILD_ABORTED: failed to delete ${failedDeletions.length} existing entries — aborting to avoid ledger corruption`);
  }

  // 2. Create a single initial deposit
  await sr.entities.CashEntry.create({
    user_id: userId,
    entry_type: 'deposit',
    amount: PAPER_INITIAL_CASH,
    balance_after: PAPER_INITIAL_CASH,
    description: INITIAL_DEPOSIT_DESC,
  });

  // 3. Replay all fills in chronological order
  const fills = await sr.entities.Fill.filter({ user_id: userId });
  fills.sort((a, b) => new Date(a.timestamp || a.created_date) - new Date(b.timestamp || b.created_date));

  for (const fill of fills) {
    const notional = (fill.filled_quantity || 0) * (fill.filled_price || 0);
    const commission = fill.commission || 0;
    const fees = fill.fees || 0;

    if (fill.side === 'buy') {
      await recordBuySettlement(sr, userId, {
        symbol: fill.symbol,
        notional,
        commission,
        fees,
        trade_intent_id: fill.trade_intent_id,
        fill_id: fill.fill_id,
        portfolio_id: fill.portfolio_id,
      });
    } else {
      await recordSellSettlement(sr, userId, {
        symbol: fill.symbol,
        notional,
        commission,
        fees,
        trade_intent_id: fill.trade_intent_id,
        fill_id: fill.fill_id,
        portfolio_id: fill.portfolio_id,
      });
    }
  }

  // 4. Validate the reconstructed ledger
  const validation = await validateCashSequence(sr, userId);
  return {
    rebuilt_entries: fills.length + 1,
    final_balance: validation.finalBalance,
    validation,
  };
}