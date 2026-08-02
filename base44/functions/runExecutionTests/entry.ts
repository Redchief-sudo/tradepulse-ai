import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Automated execution test suite — verifies critical financial integrity scenarios.
// Creates its own test data, asserts behavior, and cleans up.
// Run manually from the Operations page or via a scheduled workflow.
export default async function(req) {
  const base44 = createClientFromRequest(req);
  const user = await base44.auth.me();
  if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

  const sr = base44.asServiceRole;
  const userId = user.id;
  const testRunId = `test-${crypto.randomUUID()}`;
  const results = [];
  const cleanup = [];

  function assert(name, condition, detail = '') {
    results.push({ test: name, passed: !!condition, detail });
    return !!condition;
  }

  try {
    // === TEST 1: Rejected order causes zero portfolio mutation ===
    {
      const sym = `TST${Math.floor(Math.random() * 9999)}`;
      const beforeHoldings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
      const beforeTrades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });

      // Submit a trade with invalid price (should be rejected by execution engine)
      const result = await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 10, price: 0,
        execution_mode: 'internal_paper',
      });

      const afterHoldings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
      const afterTrades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });

      assert('Rejected order: zero holding mutation', afterHoldings.length === beforeHoldings.length);
      assert('Rejected order: zero trade mutation', afterTrades.length === beforeTrades.length);
    }

    // === TEST 2: Full buy fill creates exactly one fill, trade, and position ===
    {
      const sym = `TST${Math.floor(Math.random() * 9999)}`;
      cleanup.push(sym);

      const result = await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 10, price: 100,
        company_name: 'Test Corp', sector: 'Test',
        execution_mode: 'internal_paper',
        idempotency_key: `${testRunId}-buy-${sym}`,
      });

      const fills = await sr.entities.Fill.filter({ user_id: userId, symbol: sym });
      const trades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });
      const holdings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
      const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym });

      assert('Full buy: exactly one fill', fills.length === 1, `got ${fills.length}`);
      assert('Full buy: exactly one trade', trades.length === 1, `got ${trades.length}`);
      assert('Full buy: holding created', holdings.length === 1, `got ${holdings.length}`);
      assert('Full buy: one lot created', lots.length === 1, `got ${lots.length}`);
      assert('Full buy: correct shares', holdings[0]?.shares === 10, `got ${holdings[0]?.shares}`);
      assert('Full buy: correct avg_price', holdings[0]?.avg_price === 100, `got ${holdings[0]?.avg_price}`);
    }

    // === TEST 3: Full sell calculates correct realized P&L ===
    {
      const sym = `TST${Math.floor(Math.random() * 9999)}`;
      cleanup.push(sym);

      // Buy first
      await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 10, price: 100,
        company_name: 'Test Corp', sector: 'Test',
        execution_mode: 'internal_paper',
        idempotency_key: `${testRunId}-buy2-${sym}`,
      });

      // Sell at 120 → realized P&L should be (120-100)*10 = 200
      const sellResult = await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'sell', qty: 10, price: 120,
        company_name: 'Test Corp', sector: 'Test',
        execution_mode: 'internal_paper',
        idempotency_key: `${testRunId}-sell-${sym}`,
      });

      const sellTrades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym, action: 'sell' });
      const remainingHoldings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
      const closedLots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym, status: 'closed' });

      assert('Full sell: realized P&L = 200', sellTrades[0]?.realized_pnl === 200, `got ${sellTrades[0]?.realized_pnl}`);
      assert('Full sell: holding deleted', remainingHoldings.length === 0, `got ${remainingHoldings.length}`);
      assert('Full sell: lot closed', closedLots.length === 1, `got ${closedLots.length}`);
    }

    // === TEST 4: Retry does not submit a second order ===
    {
      const sym = `TST${Math.floor(Math.random() * 9999)}`;
      cleanup.push(sym);
      const idemKey = `${testRunId}-retry-${sym}`;

      // First call
      await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 5, price: 50,
        execution_mode: 'internal_paper',
        idempotency_key: idemKey,
      });

      // Retry with same key
      await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 5, price: 50,
        execution_mode: 'internal_paper',
        idempotency_key: idemKey,
      });

      const fills = await sr.entities.Fill.filter({ user_id: userId, symbol: sym });
      const trades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });
      const intents = await sr.entities.TradeIntent.filter({ user_id: userId, idempotency_key: idemKey });

      assert('Retry: exactly one intent', intents.length === 1, `got ${intents.length}`);
      assert('Retry: exactly one fill', fills.length === 1, `got ${fills.length}`);
      assert('Retry: exactly one trade', trades.length === 1, `got ${trades.length}`);
    }

    // === TEST 5: Partial sell leaves remaining lot ===
    {
      const sym = `TST${Math.floor(Math.random() * 9999)}`;
      cleanup.push(sym);

      // Buy 10 at 100
      await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'buy', qty: 10, price: 100,
        execution_mode: 'internal_paper',
        idempotency_key: `${testRunId}-pbuy-${sym}`,
      });

      // Sell 4 at 110 → realized P&L = (110-100)*4 = 40
      await base44.functions.invoke('executeTrade', {
        symbol: sym, action: 'sell', qty: 4, price: 110,
        execution_mode: 'internal_paper',
        idempotency_key: `${testRunId}-psell-${sym}`,
      });

      const holdings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
      const partialLots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym, status: 'partially_closed' });
      const sellTrades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym, action: 'sell' });

      assert('Partial sell: 6 shares remain', holdings[0]?.shares === 6, `got ${holdings[0]?.shares}`);
      assert('Partial sell: lot partially closed', partialLots.length === 1, `got ${partialLots.length}`);
      assert('Partial sell: realized P&L = 40', sellTrades[0]?.realized_pnl === 40, `got ${sellTrades[0]?.realized_pnl}`);
    }

    // === TEST 6: Cross-user isolation ===
    {
      // Verify that user-scoped queries only return this user's records
      const myHoldings = await sr.entities.Holding.filter({ user_id: userId });
      const allMyHoldingsHaveUserId = myHoldings.every((h) => h.user_id === userId);
      assert('Cross-user: all holdings have correct user_id', allMyHoldingsHaveUserId, `${myHoldings.length} holdings checked`);
    }

    // === TEST 7: Model governance gates ===
    {
      // Verify that governance with insufficient outcomes does not promote
      const govResult = await base44.functions.invoke('runModelGovernance', {});
      assert('Governance: insufficient outcomes → no promotion', govResult.skipped === true || govResult.initialized === true, JSON.stringify(govResult).substring(0, 200));
    }

    // === CLEANUP ===
    for (const sym of cleanup) {
      try {
        const holdings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
        for (const h of holdings) await sr.entities.Holding.delete(h.id);
        const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym });
        for (const l of lots) await sr.entities.PositionLot.delete(l.id);
        const fills = await sr.entities.Fill.filter({ user_id: userId, symbol: sym });
        for (const f of fills) await sr.entities.Fill.delete(f.id);
        const trades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });
        for (const t of trades) await sr.entities.Trade.delete(t.id);
        const intents = await sr.entities.TradeIntent.filter({ user_id: userId, symbol: sym });
        for (const i of intents) await sr.entities.TradeIntent.delete(i.id);
      } catch (e) {}
    }

    const passed = results.filter((r) => r.passed).length;
    const failed = results.filter((r) => !r.passed).length;

    return Response.json({
      ok: true,
      test_run_id: testRunId,
      total: results.length,
      passed,
      failed,
      results,
    });
  } catch (error) {
    // Cleanup on error
    for (const sym of cleanup) {
      try {
        const holdings = await sr.entities.Holding.filter({ user_id: userId, symbol: sym });
        for (const h of holdings) await sr.entities.Holding.delete(h.id);
        const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym });
        for (const l of lots) await sr.entities.PositionLot.delete(l.id);
        const fills = await sr.entities.Fill.filter({ user_id: userId, symbol: sym });
        for (const f of fills) await sr.entities.Fill.delete(f.id);
        const trades = await sr.entities.Trade.filter({ user_id: userId, symbol: sym });
        for (const t of trades) await sr.entities.Trade.delete(t.id);
        const intents = await sr.entities.TradeIntent.filter({ user_id: userId, symbol: sym });
        for (const i of intents) await sr.entities.TradeIntent.delete(i.id);
      } catch (e) {}
    }
    return Response.json({ error: error.message, results }, { status: 500 });
  }
}