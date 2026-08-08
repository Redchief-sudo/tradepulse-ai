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

    // TRAILING STOP — ratchet the stop_loss up as a position gains, locking in
    // profits so a winner never turns into a loser. Only moves the stop UP.
    //   +5% gain → stop to breakeven (no loss)
    //  +10% gain → stop to +5% (lock in 5%)
    //  +15% gain → stop to +10% (lock in 10%)
    const trailingUpdates = [];
    for (const h of holdings) {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const gainPct = h.avg_price > 0 ? ((live - h.avg_price) / h.avg_price) * 100 : 0;
      if (gainPct < 5) continue;
      let trailStop = h.stop_loss || 0;
      if (gainPct >= 15) trailStop = Math.max(trailStop, h.avg_price * 1.10);
      else if (gainPct >= 10) trailStop = Math.max(trailStop, h.avg_price * 1.05);
      else if (gainPct >= 5) trailStop = Math.max(trailStop, h.avg_price * 1.00);
      trailStop = Math.round(trailStop * 100) / 100;
      if (trailStop > (h.stop_loss || 0)) {
        trailingUpdates.push({ id: h.id, stop_loss: trailStop });
        h.stop_loss = trailStop; // update in-memory for trigger detection below
      }
    }
    if (trailingUpdates.length) await sr.entities.Holding.bulkUpdate(trailingUpdates);

    // Detect triggered positions — both stop-loss AND take-profit.
    // Uses the ATR-based stop_loss/target_price stored on the Holding (set by the
    // scan cycle), falling back to the fixed % threshold if not set.
    const triggered = holdings.filter((h) => {
      const live = priceMap[h.symbol] || h.current_price || h.avg_price;
      const drop = h.avg_price > 0 ? ((h.avg_price - live) / h.avg_price) * 100 : 0;
      const hitTarget = h.target_price && live > 0 && live >= h.target_price;
      const hitStop = h.stop_loss > 0 ? live <= h.stop_loss : drop >= stopThreshold;
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

      // ADAPTIVE EXITS — partial profit-taking with trailing stops.
      // On take-profit: sell 50% of the position, raise stop to breakeven
      // on the rest, and set a higher target for the remaining shares.
      // On stop-loss: sell all remaining (full exit).
      // (Per user request: smarter adaptive exits with scale-outs.)
      let exitQty = h.shares;
      let partialExit = false;
      if (isTakeProfit && h.shares > 1) {
        exitQty = Math.round((h.shares * 0.5) * 1000) / 1000;
        partialExit = true;
        if (exitQty < 0.001) { exitQty = h.shares; partialExit = false; }
      }

      const exitReason = isTakeProfit
        ? `take-profit @ +${gainPct.toFixed(1)}% (target $${h.target_price})${partialExit ? ' — partial 50% exit' : ''}`
        : h.stop_loss > 0
          ? `stop-loss @ $${h.stop_loss} (price $${live.toFixed(2)}, ${gainPct >= 0 ? '+' : ''}${gainPct.toFixed(1)}%)`
          : `stop-loss @ -${dropPct.toFixed(1)}% (threshold ${stopThreshold}%)`;

      // A partial exit's adaptive metadata is carried on the TradeIntent and
      // projected only by the settlement processor after financial integrity
      // verification. The stop-loss caller must not mutate the Holding based
      // merely on an order/fill response.
      const targetDistance = partialExit ? h.target_price - h.avg_price : null;
      const postSettlementStop = partialExit ? h.avg_price : null;
      const postSettlementTarget = partialExit ? h.target_price + targetDistance : null;

      // ASSET-CLASS-AWARE MARKET HOURS GATE — crypto trades 24/7, so only
      // stock positions are gated by the US regular session. Crypto positions
      // can be exited at any time. (Fixes Rev.10 defects #8 + #13: the old
      // code applied the US equity market-hours gate to all asset classes,
      // blocking valid crypto exits outside 9:30 AM–4:00 PM ET.)
      const assetClass = (h.asset_class || 'stocks').toLowerCase();
      const isCrypto = assetClass === 'crypto';
      if (!marketOpen && !isCrypto) {
        results.push({ symbol: h.symbol, shares: exitQty, price: live, dropPct, gainPct, exitReason, status: 'skipped_market_closed', reason: `MARKET_CLOSED (${session})` });
        continue;
      }

      // Route through the canonical execution boundary.
      const result = await settleTrade(base44, user, {
        symbol: h.symbol,
        action: 'sell',
        qty: exitQty,
        price: live,
        company_name: h.company_name || h.symbol,
        ai_recommended: true,
        source: isTakeProfit ? 'takeprofit' : 'stoploss',
        notes: `Auto ${exitReason}`,
        idempotency_key: `${isTakeProfit ? 'tp' : 'sl'}-${h.portfolio_id || 'default'}-${h.id}-${h.symbol}-${new Date().toISOString().slice(0, 13)}`,
        signal_timestamp: new Date().toISOString(),
        finnhub_key: key,
        stop_loss: postSettlementStop,
        target_price: postSettlementTarget,
      });
      const rStatus = result?.status || result?.settlement?.status;
      // Classify the result status — only confirmed fills are "sold".
      let status = 'submitted';
      if (rStatus === 'filled' || rStatus === 'paper_filled') status = 'filled';
      else if (rStatus === 'rejected' || rStatus === 'failed') status = 'rejected';
      else if (rStatus === 'canceled' || rStatus === 'expired') status = 'canceled';
      else if (rStatus === 'partially_filled') status = 'partially_filled';
      else if (rStatus === 'pending' || rStatus === 'submitted' || rStatus === 'accepted') status = 'pending';
      results.push({ symbol: h.symbol, shares: exitQty, price: live, dropPct, gainPct, exitReason, status, settlement: result });
    }

    // Email + Telegram alert
    if (results.length) {
      // Only confirmed fills are labeled "SOLD". Skipped, pending, rejected,
      // and canceled results get accurate status labels. (Fixes Rev.10
      // defect #11: notifications said "SOLD" for all results including
      // skipped and pending orders.)
      const statusLabel = (r) => {
        if (r.status === 'filled') return 'SOLD';
        if (r.status === 'skipped_market_closed') return 'SKIPPED (market closed)';
        if (r.status === 'pending') return 'PENDING';
        if (r.status === 'rejected') return 'REJECTED';
        if (r.status === 'canceled') return 'CANCELED';
        if (r.status === 'partially_filled') return 'PARTIALLY FILLED';
        return 'SUBMITTED';
      };
      const body = results
        .map((r) => `${statusLabel(r)} ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} — ${r.exitReason}`)
        .join('\n');
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse Alert: ${results.length} position(s) auto-exited`,
          body,
        });
      } catch (e) {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', entity_type: 'Holding', message: `Stop-loss email failed: ${e.message}` });
      }

      // Telegram alert (if enabled)
      if (user.telegram_chat_id && user.telegram_notifications_enabled) {
        try {
          const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
          if (botToken) {
            const lines = results.map((r) => {
              const label = r.status === 'filled' ? '🔴 SOLD' : `⚠️ ${r.status.toUpperCase()}`;
              return `${label} ${r.shares} ${r.symbol} @ $${r.price.toFixed(2)} — ${r.exitReason}`;
            });
            await sendTelegramMessage(
              botToken,
              String(user.telegram_chat_id),
              `🤖 <b>TradePulse AI</b> auto-exited ${results.length} position(s):\n${lines.join('\n')}`
            );
          }
        } catch (e) {
          await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', entity_type: 'Holding', message: `Stop-loss Telegram failed: ${e.message}` });
        }
      }
    }

    return Response.json({ ok: true, checked: holdings.length, triggered: results });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
