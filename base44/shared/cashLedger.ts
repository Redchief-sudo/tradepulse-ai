// Internal paper cash ledger — simulates cash accounting for internal paper mode.
// Tracks deposits, withdrawals, trade settlements, commissions, and fees so
// that paper trading has accurate buying power and cash balance, not just
// position value.
//
// For broker_paper/live mode, the broker's account endpoint is authoritative.
// This ledger is ONLY used for internal_paper mode (no broker connected).
//
// IDEMPOTENCY: trade-related entries (buy/sell/commission/fee) are deduplicated
// by fill_id + entry_type — a retry will not double-apply cash. (Fixes Rev.12 #6.)
// INITIAL DEPOSIT: the first call initializes the account with $100k. A
// description-based check prevents duplicate initial deposits. (Fixes Rev.12 #7.)

const PAPER_INITIAL_CASH = 100000; // $100,000 default paper account
const INITIAL_DEPOSIT_DESC = 'Initial paper account deposit';

// Get the current cash balance for a user (internal paper mode).
// Returns 0 if no entries exist — caller must initialize if needed.
export async function getCashBalance(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  return entries.reduce((sum, e) => sum + (e.amount || 0), 0);
}

// Initialize the paper account with the default deposit. Idempotent — checks
// for an existing initial deposit before creating one. (Fixes Rev.12 #7.)
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

// Record a cash entry with idempotency by fill_id. (Fixes Rev.12 #6.)
// If a fill_id is provided and a CashEntry already exists for that fill_id +
// entry_type, the call is a no-op and returns the existing balance.
// THROWS on failure — cash settlement is mandatory, not best-effort.
export async function recordCashEntry(sr, userId, entry) {
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

  // Initialize the paper account on first use if no entries exist
  const allEntries = await sr.entities.CashEntry.filter({ user_id: userId });
  if (allEntries.length === 0) {
    await sr.entities.CashEntry.create({
      user_id: userId,
      entry_type: 'deposit',
      amount: PAPER_INITIAL_CASH,
      balance_after: PAPER_INITIAL_CASH,
      description: INITIAL_DEPOSIT_DESC,
    });
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

// Get buying power for internal paper mode = cash balance (no margin in paper mode).
export async function getPaperBuyingPower(sr, userId) {
  // Ensure the account is initialized
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  if (entries.length === 0) {
    await initializePaperAccount(sr, userId);
  }
  return await getCashBalance(sr, userId);
}

// Get total paper equity = cash balance + position market value. (Fixes Rev.12 #8.)
export async function getPaperEquity(sr, userId, holdingsValue) {
  const cash = await getPaperBuyingPower(sr, userId);
  return cash + (holdingsValue || 0);
}

// Record a buy settlement: cash decreases by notional + commission + fees.
export async function recordBuySettlement(sr, userId, params) {
  const { symbol, notional, commission = 0, fees = 0, trade_intent_id, fill_id, portfolio_id } = params;
  const totalCost = -(notional + commission + fees);
  return await recordCashEntry(sr, userId, {
    portfolio_id,
    entry_type: 'buy',
    amount: totalCost,
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