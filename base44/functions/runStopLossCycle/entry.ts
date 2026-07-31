import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { fetchQuotes as fetchMultiAssetQuotes } from '../../shared/marketDataAdapter.ts';

// Admin / scheduled autonomous stop-loss monitor.
// 1. Refreshes real prices from Finnhub for every holding.
// 2. Auto-sells any position down >= stop_loss_pct from its avg buy price.
//    - Routes through Alpaca if the user has it connected (paper or live).
//    - Otherwise records a paper sell trade.
// 3. Emails the user a summary of any auto-exits.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') {
      return Response.json({ error: 'Admin only' }, { status: 403 });
    }

    const key = secrets.get('FINNHUB_API_KEY');
    const holdings = await base44.asServiceRole.entities.Holding.list();
    if (!holdings.length) return Response.json({ ok: true, checked: 0, triggered: [] });

    const hasStocks = holdings.some((h) => (h.asset_class || 'stocks') === 'stocks');
    if (!key && hasStocks) return Response.json({ error: 'FINNHUB_API_KEY not set' }, { status: 500 });

    const threshold = user.stop_loss_pct || 8;
    const items = holdings.map((h) => ({ symbol: h.symbol, asset_class: h.asset_class || 'stocks' }));
    const quotes = await fetchMultiAssetQuotes(items, key);
    const priceMap = {};
    const changeMap = {};
    quotes.filter((q) => !q.error).forEach((q) => {
      priceMap[q.symbol.toUpperCase()] = q.price;
      changeMap[q.symbol.toUpperCase()] = q.day_change_percent;
    });

    // Persist refreshed prices
    const updates = holdings
      .filter((h) => priceMap[h.symbol])
      .map((h) => ({
        id: h.id,
        current_price: priceMap[h.symbol],
        day_change_percent: changeMap[h.symbol] || 0,
      }));
    if (updates.length) await base44.asServiceRole.entities.Holding.bulkUpdate(updates);

    // Detect triggered positions
    const triggered = holdings.filter((h) => {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const drop = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;
      return drop >= threshold;
    });

    const results = [];
    for (const h of triggered) {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const dropPct = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;

      // Route through the canonical execution boundary. Accounting is settled
      // ONLY from a confirmed broker fill — a rejected order leaves the holding intact.
      const result = await settleTrade(base44, user, {
        symbol: h.symbol,
        action: 'sell',
        qty: h.shares,
        price: live,
        company_name: h.company_name || h.symbol,
        ai_recommended: true,
        source: 'stoploss',
        notes: `Auto stop-loss @ -${dropPct.toFixed(1)}% (threshold ${threshold}%)`,
      });
      results.push({ symbol: h.symbol, shares: h.shares, price: live, dropPct, settlement: result });
    }

    // Email alert
    if (results.length) {
      try {
        const body = results
          .map((r) => `SOLD ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} (down ${r.dropPct.toFixed(1)}%)`)
          .join('\n');
        await base44.asServiceRole.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse Alert: ${results.length} stop-loss position(s) auto-exited`,
          body,
        });
      } catch (e) {
        // email failure shouldn't fail the cycle
      }
    }

    return Response.json({ ok: true, checked: holdings.length, triggered: results });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}