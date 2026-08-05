// Internal paper cash ledger — simulates cash accounting for internal paper mode.
// Tracks deposits, withdrawals, trade settlements, commissions, and fees so
// that paper trading has accurate buying power and cash balance, not just
// position value.
//
// For broker_paper/live mode, the broker's account endpoint is authoritative.
// This ledger is ONLY used for internal_paper mode (no broker connected).

const PAPER_INITIAL_CASH = 100000; // $100,000 default paper account

// Get the current cash balance for a user (internal paper mode).
// Computed from the sum of all CashEntry amounts. If no entries exist,
// returns the initial paper cash.
export async function getCashBalance(sr, userId) {
  const entries = await sr.entities.CashEntry.filter({ user_id: userId });
  if (entries.length === 0) return PAPER_INITIAL_CASH;
  return entries.reduce((sum, e) => sum + (e.amount || 0), 0);
}

// Record a cash entry and return the new balance.
// Creates the initial deposit on first call if no entries exist.
export async function recordCashEntry(sr, userId, entry) {
  let balance = await getCashBalance(sr, userId);
  // If this is the first entry and it's not a deposit, create the initial deposit first.
  if (balance === PAPER_INITIAL_CASH) {
    const existing = await sr.entities.CashEntry.filter({ user_id: userId });
    if (existing.length === 0) {
      await sr.entities.CashEntry.create({
        user_id: userId,
        entry_type: 'deposit',
        amount: PAPER_INITIAL_CASH,
        balance_after: PAPER_INITIAL_CASH,
        description: 'Initial paper account deposit',
      });
    }
  }
  balance = await getCashBalance(sr, userId);
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
  return await getCashBalance(sr, userId);
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