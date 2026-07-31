import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Broker-authoritative reconciliation. The broker is the source of truth for positions;
// the app ledger is the analytical history. This compares broker positions to app
// Holdings and records a ReconciliationEvent for EVERY comparison (matched or drift),
// never silently deleting discrepancies. Externally-closed positions are recorded as
// sell trades (with realized P&L + provenance) before the Holding is removed.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (user.broker !== 'alpaca' || !user.broker_api_key || !user.broker_api_secret) {
      return Response.json({ error: 'Alpaca not connected — nothing to reconcile' }, { status: 400 });
    }
    const sr = base44.asServiceRole;
    const runTs = new Date().toISOString();

    const base = user.broker_mode === 'live' ? 'https://api.alpaca.markets/v2' : 'https://paper-api.alpaca.markets/v2';
    let brokerPositions = [];
    try {
      const res = await fetch(`${base}/positions`, {
        headers: { 'APCA-API-KEY-ID': user.broker_api_key, 'APCA-API-SECRET-KEY': user.broker_api_secret },
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        await sr.entities.ReconciliationEvent.create({ run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: `Alpaca HTTP ${res.status}: ${txt}`, action_taken: 'flagged_for_review' });
        return Response.json({ error: `Alpaca HTTP ${res.status}: ${txt}` }, { status: 502 });
      }
      brokerPositions = await res.json();
    } catch (e) {
      await sr.entities.ReconciliationEvent.create({ run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: e.message, action_taken: 'flagged_for_review' });
      return Response.json({ error: e.message }, { status: 502 });
    }

    const brokerMap = {};
    brokerPositions.forEach((p) => {
      brokerMap[String(p.symbol).toUpperCase()] = {
        symbol: p.symbol, shares: Number(p.qty), avg_price: Number(p.avg_entry_price),
        current_price: Number(p.current_price), market_value: Number(p.market_value),
      };
    });

    const appHoldings = await sr.entities.Holding.list();
    const appMap = {};
    appHoldings.forEach((h) => { appMap[String(h.symbol).toUpperCase()] = h; });

    const events = [];
    const created = [], updated = [], removed = [];

    // broker -> app
    for (const [sym, bp] of Object.entries(brokerMap)) {
      const existing = appMap[sym];
      if (!existing) {
        await sr.entities.Holding.create({
          symbol: bp.symbol, company_name: bp.symbol, shares: bp.shares, avg_price: bp.avg_price,
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

    // app -> broker (externally closed) — record the sell trade BEFORE removing.
    for (const [sym, h] of Object.entries(appMap)) {
      if (!brokerMap[sym]) {
        const exitPrice = h.current_price || h.avg_price;
        const realizedPnl = (exitPrice - h.avg_price) * h.shares;
        await sr.entities.Trade.create({
          symbol: h.symbol, company_name: h.company_name || h.symbol, action: 'sell', shares: h.shares,
          price: exitPrice, total_value: h.shares * exitPrice,
          notes: 'Reconciliation: position no longer held at broker (externally closed)',
          ai_recommended: false, order_status: 'reconciled_external', source: 'reconciliation', realized_pnl: realizedPnl,
        });
        await sr.entities.Holding.delete(h.id);
        removed.push(sym);
        events.push({ event_type: 'externally_closed', symbol: sym, app_qty: h.shares, app_avg_price: h.avg_price, action_taken: 'recorded_sell_and_removed', realized_pnl: realizedPnl });
      }
    }

    // Persist every reconciliation event for audit history.
    for (const e of events) {
      await sr.entities.ReconciliationEvent.create({ run_timestamp: runTs, ...e });
    }

    const summary = {
      matched: events.filter((e) => e.event_type === 'matched').length,
      qty_drift: events.filter((e) => e.event_type === 'qty_drift').length,
      price_drift: events.filter((e) => e.event_type === 'price_drift').length,
      new_from_broker: created.length,
      externally_closed: removed.length,
    };

    return Response.json({ ok: true, run_timestamp: runTs, broker_positions: brokerPositions.length, created: created.length, updated: updated.length, removed, events: events.length, summary });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}