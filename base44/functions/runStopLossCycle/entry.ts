import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { fetchQuotes as fetchMultiAssetQuotes } from '../../shared/marketDataAdapter.ts';
import { isUsMarketOpen, usMarketSession } from '../../shared/marketHours.ts';
import { sendTelegramMessage } from '../../shared/telegram.ts';

// Admin / scheduled autonomous position-exit monitor.
// USER-SCOPED: all queries filter by user_id.
// 1. Refreshes real prices for every holding.
// 2. Auto-sells any position down >= stop_loss_pct from its avg buy price (stop-loss).
// 3. Auto-sells any position that reached its target_price (take-profit).
// 4. Alerts the user (email + Telegram) with a summary of any auto-exits.
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

    const stopThreshold = user.stop_loss_pct || 8;
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

    // Detect triggered positions — both stop-loss AND take-profit
    const triggered = holdings.filter((h) => {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const drop = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;
      const hitTarget = h.target_price && live > 0 && live >= h.target_price;
      const hitStop = drop >= stopThreshold;
      return hitStop || hitTarget;
    });

    // MARKET HOURS GATE: only submit broker orders during the US regular session.
    // We still refresh prices and detect triggers outside market hours (so the
    // dashboard and alerts stay current), but we do NOT route sell orders to the
    // broker when the session is closed.
    const marketOpen = isUsMarketOpen();
    const session = usMarketSession();
    const results = [];
    for (const h of triggered) {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const dropPct = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;
      const gainPct = h.avg_price > 0 ? ((live - h.avg_price) / h.avg_price) * 100 : 0;
      const isTakeProfit = h.target_price && live >= h.target_price;
      const exitReason = isTakeProfit
        ? `take-profit @ +${gainPct.toFixed(1)}% (target $${h.target_price})`
        : `stop-loss @ -${dropPct.toFixed(1)}% (threshold ${stopThreshold}%)`;

      if (!marketOpen) {
        results.push({ symbol: h.symbol, shares: h.shares, price: live, dropPct, gainPct, exitReason, skipped: true, reason: `MARKET_CLOSED (${session})` });
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
        source: isTakeProfit ? 'takeprofit' : 'stoploss',
        notes: `Auto ${exitReason}`,
        idempotency_key: `${isTakeProfit ? 'tp' : 'sl'}-${h.portfolio_id || 'default'}-${h.id}-${h.symbol}-${new Date().toISOString().slice(0, 13)}`,
        signal_timestamp: new Date().toISOString(),
        finnhub_key: key,
      });
      results.push({ symbol: h.symbol, shares: h.shares, price: live, dropPct, gainPct, exitReason, settlement: result });
    }

    // Email + Telegram alert
    if (results.length) {
      const body = results
        .map((r) => `SOLD ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} — ${r.exitReason}`)
        .join('\n');
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse Alert: ${results.length} position(s) auto-exited`,
          body,
        });
      } catch (e) { /* email failure shouldn't fail the cycle */ }

      // Telegram alert (if enabled)
      if (user.telegram_chat_id && user.telegram_notifications_enabled) {
        try {
          const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
          if (botToken) {
            const lines = results.map((r) => `🔴 SOLD ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} — ${r.exitReason}`);
            await sendTelegramMessage(
              botToken,
              String(user.telegram_chat_id),
              `🤖 <b>TradePulse AI</b> auto-exited ${results.length} position(s):\n${lines.join('\n')}`
            );
          }
        } catch (e) { /* non-fatal */ }
      }
    }

    return Response.json({ ok: true, checked: holdings.length, triggered: results });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}