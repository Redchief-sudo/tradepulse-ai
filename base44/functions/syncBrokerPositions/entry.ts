import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaActivities } from '../../shared/alpaca.ts';

// Broker-authoritative reconciliation. The broker is the source of truth for positions.
// USER-SCOPED: all queries filter by user_id. Credentials read from BrokerCredential.
// ACTUAL CLOSE PRICES: for externally closed positions, fetch Alpaca fill activities
// to find the real exit price — never invent one. If no activity is found, record as
// reconciliation_adjustment with unknown exit price rather than fabricating one.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const runTs = new Date().toISOString();

    // Read credentials from the secure BrokerCredential entity
    const creds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (!creds[0] || creds[0].broker !== 'alpaca') {
      return Response.json({ error: 'Alpaca not connected — nothing to reconcile' }, { status: 400 });
    }
    const cred = creds[0];
    const base = cred.mode === 'live' ? 'https://api.alpaca.markets/v2' : 'https://paper-api.alpaca.markets/v2';
    const hdrs = { 'APCA-API-KEY-ID': cred.api_key, 'APCA-API-SECRET-KEY': cred.api_secret };

    let brokerPositions = [];
    try {
      const res = await fetch(`${base}/positions`, { headers: hdrs });
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: `Alpaca HTTP ${res.status}: ${txt}`, action_taken: 'flagged_for_review' });
        return Response.json({ error: `Alpaca HTTP ${res.status}: ${txt}` }, { status: 502 });
      }
      brokerPositions = await res.json();
    } catch (e) {
      await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: e.message, action_taken: 'flagged_for_review' });
      return Response.json({ error: e.message }, { status: 502 });
    }

    const brokerMap = {};
    brokerPositions.forEach((p) => {
      brokerMap[String(p.symbol).toUpperCase()] = {
        symbol: p.symbol, shares: Number(p.qty), avg_price: Number(p.avg_entry_price),
        current_price: Number(p.current_price), market_value: Number(p.market_value),
      };
    });

    // USER-SCOPED: only this user's holdings
    const appHoldings = await sr.entities.Holding.filter({ user_id: user.id });
    const appMap = {};
    appHoldings.forEach((h) => { appMap[String(h.symbol).toUpperCase()] = h; });

    const events = [];
    const created = [], updated = [], removed = [];

    // broker -> app
    for (const [sym, bp] of Object.entries(brokerMap)) {
      const existing = appMap[sym];
      if (!existing) {
        await sr.entities.Holding.create({
          user_id: user.id, symbol: bp.symbol, company_name: bp.symbol, shares: bp.shares, avg_price: bp.avg_price,
          current_price: bp.current_price, sector: '', day_change_percent: 0,
        });
        created.push(sym);
        events.push({ event_type: 'new_from_broker', symbol: sym, broker_qty: bp.shares, broker_avg_price: bp.avg_price, action_taken: 'created_holding' });
      } else {
        const qtyDrift = Math.abs((existing.shares || 0) - bp.shares) > 0.0001;
        const priceDrift = existing.current_price && Math.abs(existing.current_price - bp.current_price) > 0.01;
        if (qtyDrift) {
          await sr.entities.Holding.update(existing.id, { shares: bp.shares, avg_price: bp.avg_price, current_price: bp.current_price });
          updated.push({ symbol: sym, from: existing.shares, to: bp.shares });
          events.push({ event_type: 'qty_drift', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, app_avg_price: existing.avg_price, broker_avg_price: bp.avg_price, action_taken: 'updated_holding' });
        } else if (priceDrift) {
          await sr.entities.Holding.update(existing.id, { current_price: bp.current_price });
          events.push({ event_type: 'price_drift', symbol: sym, app_current_price: existing.current_price, broker_current_price: bp.current_price, action_taken: 'updated_holding' });
        } else {
          events.push({ event_type: 'matched', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, action_taken: 'none' });
        }
      }
    }

    // app -> broker (externally closed) — fetch ACTUAL close price from Alpaca activities.
    // If no fill activity is found, record as reconciliation_adjustment with unknown exit.
    for (const [sym, h] of Object.entries(appMap)) {
      if (!brokerMap[sym]) {
        let exitPrice = null;
        let realizedPnl = null;
        let actionTaken = 'recorded_sell_and_removed';
        let eventType = 'externally_closed';

        try {
          // Fetch recent fill activities to find the actual close transaction
          const sinceDate = new Date(h.created_date || Date.now() - 30 * 86400000).toISOString().slice(0, 10);
          const activities = await getAlpacaActivities({ apiKey: cred.api_key, secretKey: cred.api_secret, mode: cred.mode, sinceDate });
          // Multi-criteria matching: match by order_id, client_order_id, or
          // qty proximity — never just by symbol. When no exact match can be
          // established, the system records an unresolved adjustment rather
          // than inventing a price (fail-closed behavior preserved below).
          const closeActivity = activities.find((a) => {
            if (String(a.symbol).toUpperCase() !== sym) return false;
            if (a.side !== 'sell') return false;
            // Best: match by broker order ID
            if (a.order_id && h.broker_order_id && a.order_id === h.broker_order_id) return true;
            // Good: match by client order ID
            if (a.order_client_id && h.client_order_id && a.order_client_id === h.client_order_id) return true;
            // Fallback: match by qty proximity (within 0.01 shares)
            if (a.qty && Math.abs(Number(a.qty) - h.shares) < 0.01) return true;
            // No match — do NOT fall back to symbol-only match
            return false;
          });
          if (closeActivity) {
            exitPrice = Number(closeActivity.price);
            realizedPnl = (exitPrice - h.avg_price) * h.shares;
          } else {
            // No fill activity found — do NOT invent a price. Record as adjustment.
            eventType = 'reconciliation_adjustment';
            actionTaken = 'flagged_for_review_no_exit_price';
          }
        } catch (e) {
          // Activities fetch failed — do NOT invent a price. Record as adjustment.
          eventType = 'reconciliation_adjustment';
          actionTaken = 'flagged_for_review_activities_unreachable';
        }

        if (exitPrice) {
          await sr.entities.Trade.create({
            user_id: user.id, symbol: h.symbol, company_name: h.company_name || h.symbol, action: 'sell', shares: h.shares,
            price: exitPrice, total_value: h.shares * exitPrice,
            notes: 'Reconciliation: position no longer held at broker (externally closed)',
            ai_recommended: false, order_status: 'reconciled_external', source: 'reconciliation', realized_pnl: realizedPnl,
          });
        }

        await sr.entities.Holding.delete(h.id);
        removed.push(sym);
        events.push({ event_type: eventType, symbol: sym, app_qty: h.shares, app_avg_price: h.avg_price, action_taken: actionTaken, realized_pnl: realizedPnl, details: exitPrice ? `Exit price from broker fill: $${exitPrice.toFixed(2)}` : 'No broker fill activity found — exit price unknown' });
      }
    }

    // Persist every reconciliation event (user-scoped)
    for (const e of events) {
      await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, ...e });
    }

    const summary = {
      matched: events.filter((e) => e.event_type === 'matched').length,
      qty_drift: events.filter((e) => e.event_type === 'qty_drift').length,
      price_drift: events.filter((e) => e.event_type === 'price_drift').length,
      new_from_broker: created.length,
      externally_closed: removed.length,
      adjustments: events.filter((e) => e.event_type === 'reconciliation_adjustment').length,
    };

    return Response.json({ ok: true, run_timestamp: runTs, broker_positions: brokerPositions.length, created: created.length, updated: updated.length, removed, events: events.length, summary });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}