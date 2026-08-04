import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { fetchQuotes as fetchMultiAssetQuotes } from '../../shared/marketDataAdapter.ts';
import { isUsMarketOpen, usMarketSession } from '../../shared/marketHours.ts';

// Admin / scheduled autonomous stop-loss monitor.
// USER-SCOPED: all queries filter by user_id.
// 1. Refreshes real prices for every holding.
// 2. Auto-sells any position down >= stop_loss_pct from its avg buy price.
// 3. Emails the user a summary of any auto-exits.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') {
      return Response.json({ error: 'Admin only' }, { status: 403 });
    }

    const sr = base44.asServiceRole;
    const key = secrets.get('FINNHUB_API_KEY');
    // USER-SCOPED: only this user's holdings
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
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
    if (updates.length) await sr.entities.Holding.bulkUpdate(updates);

    // Detect triggered positions
    const triggered = holdings.filter((h) => {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const drop = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;
      return drop >= threshold;
    });

    // MARKET HOURS GATE: only submit broker orders during the US regular session.
      // A day market order submitted after hours can queue and fill at a gapped
      // next open. We still refresh prices and detect triggers outside market
      // hours (so the dashboard and alerts stay current), but we do NOT route
      // sell orders to the broker when the session is closed. The execution
      // gateway independently re-checks this, but we short-circuit here too so
      // the intent is never created with a stale after-hours trigger price.
    const marketOpen = isUsMarketOpen();
    const session = usMarketSession();
    const results = [];
    for (const h of triggered) {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const dropPct = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;

      if (!marketOpen) {
        results.push({ symbol: h.symbol, shares: h.shares, price: live, dropPct, skipped: true, reason: `MARKET_CLOSED (${session})` });
        continue;
      }

      // Route through the canonical execution boundary.
      const result = await settleTrade(base44, user, {
        symbol: h.symbol,
        action: 'sell',
        qty: h.shares,
        price: live,
        company_name: h.company_name || h.symbol,
        ai_recommended: true,
        source: 'stoploss',
        notes: `Auto stop-loss @ -${dropPct.toFixed(1)}% (threshold ${threshold}%)`,
        idempotency_key: `stoploss-${h.portfolio_id || 'default'}-${h.id}-${h.symbol}-${new Date().toISOString().slice(0, 13)}`,
        signal_timestamp: new Date().toISOString(),
        finnhub_key: key,
      });
      results.push({ symbol: h.symbol, shares: h.shares, price: live, dropPct, settlement: result });
    }

    // Email alert
    if (results.length) {
      try {
        const body = results
          .map((r) => `SOLD ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} (down ${r.dropPct.toFixed(1)}%)`)
          .join('\n');
        await sr.integrations.Core.SendEmail({
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