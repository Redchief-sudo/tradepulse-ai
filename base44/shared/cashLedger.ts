// Internal paper cash ledger — simulates cash accounting for internal paper mode.
// Tracks deposits, withdrawals, trade settlements, commissions, fees, and order
// reservations so that paper trading has accurate buying power and cash balance.
//
// For broker_paper/live mode, the broker's account endpoint is authoritative.
// This ledger is ONLY used for internal_paper mode (no broker connected).
//
// REV.15 CONCURRENCY FIXES:
// The previous check-then-create pattern could not guarantee idempotency under
// concurrent writes. Two workers could both pass the check and both create.
// The create-then-dedup pattern narrows the race: both create, both query, the
// earliest record wins and the duplicate is deleted. This is the best available
// mitigation on a BaaS platform without DB-level unique constraints.
//
// #3 CASH INIT: initializePaperAccount uses create-then-dedup. A race creates
//    two opening entries; the dedup pass keeps the earliest and deletes the rest.
// #4 CASH ENTRY IDEMPOTENCY: recordCashEntry uses create-then-dedup by fill_id +
//    entry_type. A retried settlement skips already-recorded entries.
// #7 IMMUTABLE OPENING: The opening balance is stored as an 'account_opening'
//    entry type — not a hardcoded constant. rebuildCashFromFills reads the
//    actual opening balance from this immutable entry.
// #8 REBUILD WITHOUT AUTH: rebuildCashFromFills replays fills as raw entries
//    without negative-cash checks. Historical replay must reconstruct, not
//    re-authorize.
//
// NOTE: balance_after is informational only. Under concurrent writes it may be
// stale (two workers read the same balance and both write the same balance_after).
// The authoritative balance is always sum(amount) across all entries, computed
// by getCashBalance().

const DEFAULT_OPENING_CASH = 1000; // Default opening balance for new paper accounts
const OPENING_DESC = 'Account opening balance';

// Get the current cash balance for a user (internal paper mode).
// Sums all entry amounts. Reservations (negative) and releases (positive) net to
// zero when an order completes, so the balance reflects settled cash.
export async function getCashBalance(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  return entries.reduce((sum, e) => sum + (e.amount || 0), 0);
}

// Get available cash = balance (reservations are already negative in the sum).
// This is the real buying power — it accounts for funds held for pending orders.
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
  let openReservations = 0;
  for (const tid of Object.keys(reservations)) {
    if (reservations[tid] < 0) openReservations += Math.abs(reservations[tid]);
  }
  return { available: balance, balance, openReservations };
}

// Get the immutable opening balance for a user — the account_opening entry.
// Returns null if no opening entry exists (account not yet initialized).
async function getOpeningBalance(sr, userId) {
  const openingEntries = await sr.entities.CashEntry.filter({
    user_id: userId,
    entry_type: 'account_opening',
  });
  if (openingEntries.length > 0) {
    // Dedup if a race created multiple opening entries — keep the earliest.
    if (openingEntries.length > 1) {
      const sorted = openingEntries.sort((a, b) =>
        new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
      );
      for (let i = 1; i < sorted.length; i++) {
        try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
      }
      return sorted[0].amount;
    }
    return openingEntries[0].amount;
  }
  // Backward compatibility: check for old-style initial deposit
  const oldDeposits = await sr.entities.CashEntry.filter({
    user_id: userId,
    entry_type: 'deposit',
    description: 'Initial paper account deposit',
  });
  if (oldDeposits.length > 0) return oldDeposits[0].amount;
  return null;
}

// Initialize the paper account with the default opening balance.
// Uses create-then-dedup: creates the opening entry, then checks for duplicates.
// If a race created multiple opening entries, the earliest wins and the rest
// are deleted. (Fixes Rev.15 #3: filter-then-create could not prevent a race
// from creating two opening deposits, inflating the starting capital.)
export async function initializePaperAccount(sr, userId, openingAmount) {
  const amount = openingAmount != null ? openingAmount : DEFAULT_OPENING_CASH;

  // Check for an existing opening entry first (fast path — no create needed)
  const existing = await sr.entities.CashEntry.filter({
    user_id: userId,
    entry_type: 'account_opening',
  });
  if (existing.length > 0) {
    // Dedup if needed
    if (existing.length > 1) {
      const sorted = existing.sort((a, b) =>
        new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
      );
      for (let i = 1; i < sorted.length; i++) {
        try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
      }
    }
    return existing[0].amount;
  }

  // Backward compatibility: check for old-style initial deposit
  const oldDeposits = await sr.entities.CashEntry.filter({
    user_id: userId,
    entry_type: 'deposit',
    description: 'Initial paper account deposit',
  });
  if (oldDeposits.length > 0) return oldDeposits[0].amount;

  // Create the opening entry
  const created = await sr.entities.CashEntry.create({
    user_id: userId,
    entry_type: 'account_opening',
    amount,
    balance_after: amount,
    description: OPENING_DESC,
  });

  // Create-then-dedup: check if a race created multiple opening entries
  const all = await sr.entities.CashEntry.filter({
    user_id: userId,
    entry_type: 'account_opening',
  });
  if (all.length > 1) {
    const sorted = all.sort((a, b) =>
      new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
    );
    // Keep the earliest, delete the rest
    if (sorted[0].id !== created.id) {
      // We lost the race — delete our duplicate
      try { await sr.entities.CashEntry.delete(created.id); } catch (e) {}
      return sorted[0].amount;
    }
    for (let i = 1; i < sorted.length; i++) {
      try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
    }
  }
  return amount;
}

// Ensure the account is initialized before any operation.
async function ensureInitialized(sr, userId) {
  const opening = await getOpeningBalance(sr, userId);
  if (opening == null) {
    await initializePaperAccount(sr, userId);
  }
}

// Create-then-dedup for cash entries. Creates the entry, then checks for
// duplicates by the idempotency key. If a race created duplicates, the earliest
// wins and the rest are deleted. (Fixes Rev.15 #4: check-then-create could not
// prevent duplicate entries under concurrent writes.)
//
// idempotencyKey: a function that returns the filter query for dedup
//   e.g. () => ({ fill_id: 'xxx', entry_type: 'buy' })
async function idempotentCreateCashEntry(sr, userId, entry, idempotencyQuery) {
  const created = await sr.entities.CashEntry.create({
    user_id: userId,
    portfolio_id: entry.portfolio_id || null,
    entry_type: entry.entry_type,
    amount: entry.amount,
    balance_after: entry.balance_after,
    symbol: entry.symbol || null,
    trade_intent_id: entry.trade_intent_id || null,
    fill_id: entry.fill_id || null,
    description: entry.description || '',
  });

  if (!idempotencyQuery) return created;

  // Check for duplicates
  const all = await sr.entities.CashEntry.filter({ user_id: userId, ...idempotencyQuery });
  if (all.length > 1) {
    const sorted = all.sort((a, b) =>
      new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
    );
    if (sorted[0].id !== created.id) {
      // We lost the race — delete our duplicate, return the winner's balance
      try { await sr.entities.CashEntry.delete(created.id); } catch (e) {}
      return sorted[0];
    }
    // We won — clean up duplicates
    for (let i = 1; i < sorted.length; i++) {
      try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
    }
  }
  return created;
}

// Record a cash entry with create-then-dedup idempotency by fill_id + entry_type.
// (Fixes Rev.15 #4: check-then-create could not prevent duplicate entries.)
// THROWS on failure — cash settlement is mandatory, not best-effort.
export async function recordCashEntry(sr, userId, entry) {
  await ensureInitialized(sr, userId);

  // Create-then-dedup for fill-backed entries
  if (entry.fill_id) {
    const idempQuery = { fill_id: entry.fill_id, entry_type: entry.entry_type };
    // Fast path: check if already exists
    const existing = await sr.entities.CashEntry.filter({ user_id: userId, ...idempQuery });
    if (existing.length > 0) {
      // Dedup if a race created duplicates — keep the earliest
      if (existing.length > 1) {
        const sorted = existing.sort((a, b) =>
          new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
        );
        for (let i = 1; i < sorted.length; i++) {
          try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
        }
      }
      return existing[0].balance_after;
    }

    const balance = await getCashBalance(sr, userId);
    const newBalance = balance + entry.amount;
    const created = await idempotentCreateCashEntry(sr, userId, {
      ...entry,
      balance_after: newBalance,
    }, idempQuery);
    return created.balance_after;
  }

  // Non-fill entries (deposits, adjustments) — no idempotency key
  const balance = await getCashBalance(sr, userId);
  const newBalance = balance + entry.amount;
  const created = await sr.entities.CashEntry.create({
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
  return created.balance_after || newBalance;
}

// Get buying power for internal paper mode = available cash (balance minus reservations).
export async function getPaperBuyingPower(sr, userId) {
  await ensureInitialized(sr, userId);
  const { available } = await getAvailableCash(sr, userId);
  return available;
}

// Get total paper equity = settled cash + reserved cash + position market value.
export async function getPaperEquity(sr, userId, holdingsValue) {
  await ensureInitialized(sr, userId);
  const { balance, openReservations } = await getAvailableCash(sr, userId);
  return balance + openReservations + (holdingsValue || 0);
}

// Reserve cash for a pending order. Uses create-then-dedup by trade_intent_id.
// (Fixes Rev.15 #4: check-then-create could not prevent duplicate reservations.)
export async function reserveCash(sr, userId, params) {
  const { symbol, amount, trade_intent_id, portfolio_id } = params;
  if (!trade_intent_id) throw new Error('reserveCash requires trade_intent_id');
  if (amount <= 0) throw new Error('reserveCash requires positive amount');

  await ensureInitialized(sr, userId);

  // Fast path: check if reservation already exists
  const existing = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation',
  });
  if (existing.length > 0) {
    // Dedup if a race created multiple reservations — keep the earliest
    if (existing.length > 1) {
      const sorted = existing.sort((a, b) =>
        new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
      );
      for (let i = 1; i < sorted.length; i++) {
        try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
      }
    }
    return existing[0].balance_after;
  }

  const { available } = await getAvailableCash(sr, userId);
  if (available < amount) {
    throw new Error(`INSUFFICIENT_CASH_FOR_RESERVATION: available ${available}, required ${amount}`);
  }

  const balance = await getCashBalance(sr, userId);
  const newBalance = balance - amount;
  const created = await idempotentCreateCashEntry(sr, userId, {
    portfolio_id: portfolio_id || null,
    entry_type: 'reservation',
    amount: -amount,
    balance_after: newBalance,
    symbol: symbol || null,
    trade_intent_id,
    description: `Reservation for ${symbol || 'order'}: ${amount.toFixed(2)}`,
  }, { trade_intent_id, entry_type: 'reservation' });
  return created.balance_after || newBalance;
}

// Release a cash reservation. Uses create-then-dedup by trade_intent_id.
export async function releaseCashReservation(sr, userId, params) {
  const { trade_intent_id, portfolio_id } = params;
  if (!trade_intent_id) throw new Error('releaseCashReservation requires trade_intent_id');

  // Fast path: check if already released
  const existing = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation_release',
  });
  if (existing.length > 0) {
    // Dedup if a race created duplicates — keep the earliest
    if (existing.length > 1) {
      const sorted = existing.sort((a, b) =>
        new Date(a.created_date) - new Date(b.created_date) || (a.id < b.id ? -1 : 1)
      );
      for (let i = 1; i < sorted.length; i++) {
        try { await sr.entities.CashEntry.delete(sorted[i].id); } catch (e) {}
      }
    }
    return existing[0].balance_after;
  }

  // Find the reservation amount
  const reservations = await sr.entities.CashEntry.filter({
    user_id: userId,
    trade_intent_id,
    entry_type: 'reservation',
  });
  if (reservations.length === 0) return null;

  const reservedAmount = Math.abs(reservations[0].amount);
  const balance = await getCashBalance(sr, userId);
  const newBalance = balance + reservedAmount;
  const created = await idempotentCreateCashEntry(sr, userId, {
    portfolio_id: portfolio_id || null,
    entry_type: 'reservation_release',
    amount: reservedAmount,
    balance_after: newBalance,
    trade_intent_id,
    description: `Reservation released: ${reservedAmount.toFixed(2)}`,
  }, { trade_intent_id, entry_type: 'reservation_release' });
  return created.balance_after || newBalance;
}

// Record a buy settlement: cash decreases by notional + commission + fees.
// NEGATIVE CASH PREVENTION: checks available cash before debiting and throws
// if insufficient. Note: under concurrent writes, two workers can both pass the
// check and both debit, potentially going negative. The create-then-dedup
// prevents duplicate entries for the SAME fill, but cannot prevent two DIFFERENT
// fills from overdrawing simultaneously. This is a known platform limitation.
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

// Validate the cash ledger sequence — verifies that balance_after is consistent.
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
// (Fixes Rev.15 #7, #8):
// #7: The opening balance is read from the immutable account_opening entry,
//     not a hardcoded constant. If no opening entry exists, uses the default.
// #8: Fills are replayed as RAW entries without negative-cash checks. Historical
//     replay must reconstruct, not re-authorize — a valid historical trade that
//     was affordable at the time should not fail because the current balance is
//     different.
export async function rebuildCashFromFills(sr, userId) {
  // 1. Read the opening balance BEFORE clearing entries
  const openingBalance = await getOpeningBalance(sr, userId);
  const actualOpening = openingBalance != null ? openingBalance : DEFAULT_OPENING_CASH;

  // 2. Clear all existing entries — fail closed if any deletion fails.
  const allEntries = await sr.entities.CashEntry.filter({ user_id: userId });
  const failedDeletions = [];
  for (const e of allEntries) {
    try { await sr.entities.CashEntry.delete(e.id); }
    catch (err) { failedDeletions.push({ id: e.id, error: err.message }); }
  }
  if (failedDeletions.length > 0) {
    throw new Error(`REBUILD_ABORTED: failed to delete ${failedDeletions.length} existing entries — aborting to avoid ledger corruption`);
  }

  // 3. Create the immutable opening entry
  await sr.entities.CashEntry.create({
    user_id: userId,
    entry_type: 'account_opening',
    amount: actualOpening,
    balance_after: actualOpening,
    description: OPENING_DESC,
  });

  // 4. Replay all fills as RAW entries — no negative-cash checks.
  //    (Fixes Rev.15 #8: rebuild reconstructs history, does not re-authorize.)
  const fills = await sr.entities.Fill.filter({ user_id: userId });
  fills.sort((a, b) => new Date(a.timestamp || a.created_date) - new Date(b.timestamp || b.created_date));

  let runningBalance = actualOpening;
  for (const fill of fills) {
    const notional = (fill.filled_quantity || 0) * (fill.filled_price || 0);
    const commission = fill.commission || 0;
    const fees = fill.fees || 0;
    let entryAmount;

    if (fill.side === 'buy') {
      entryAmount = -(notional + commission + fees);
    } else {
      entryAmount = notional - commission - fees;
    }

    runningBalance += entryAmount;
    await sr.entities.CashEntry.create({
      user_id: userId,
      portfolio_id: fill.portfolio_id || null,
      entry_type: fill.side === 'buy' ? 'buy' : 'sell',
      amount: entryAmount,
      balance_after: runningBalance,
      symbol: fill.symbol,
      trade_intent_id: fill.trade_intent_id || null,
      fill_id: fill.fill_id,
      description: `${fill.side === 'buy' ? 'Buy' : 'Sell'} ${fill.symbol}: ${notional.toFixed(2)}`,
    });
  }

  // 5. Validate the reconstructed ledger
  const validation = await validateCashSequence(sr, userId);
  return {
    opening_balance: actualOpening,
    rebuilt_entries: fills.length + 1,
    final_balance: validation.finalBalance,
    validation,
  };
}