import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Reconciles the app's Holding records with the user's REAL broker positions.
// Fetches live positions from Alpaca and syncs the Holding entity to match:
//  - creates holdings for broker positions not tracked in the app
//  - updates share counts to match the broker
//  - removes holdings the broker no longer holds (fully closed externally)
// This closes the broker <-> app drift gap so the app reflects the real account.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (user.broker !== 'alpaca' || !user.broker_api_key || !user.broker_api_secret) {
      return Response.json({ error: 'Alpaca not connected — nothing to reconcile' }, { status: 400 });
    }

    const base = user.broker_mode === 'live' ? 'https://api.alpaca.markets/v2' : 'https://paper-api.alpaca.markets/v2';
    const res = await fetch(`${base}/positions`, {
      headers: {
        'APCA-API-KEY-ID': user.broker_api_key,
        'APCA-API-SECRET-KEY': user.broker_api_secret,
      },
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => '');
      return Response.json({ error: `Alpaca HTTP ${res.status}: ${txt}` }, { status: 502 });
    }
    const brokerPositions = await res.json();
    const brokerMap = {};
    brokerPositions.forEach((p) => {
      brokerMap[String(p.symbol).toUpperCase()] = {
        symbol: p.symbol,
        shares: Number(p.qty),
        avg_price: Number(p.avg_entry_price),
        current_price: Number(p.current_price),
        market_value: Number(p.market_value),
      };
    });

    const sr = base44.asServiceRole;
    const appHoldings = await sr.entities.Holding.list();
    const appMap = {};
    appHoldings.forEach((h) => { appMap[String(h.symbol).toUpperCase()] = h; });

    const created = [];
    const updated = [];
    const removed = [];

    // Sync from broker -> app
    for (const [sym, bp] of Object.entries(brokerMap)) {
      const existing = appMap[sym];
      if (!existing) {
        const h = await sr.entities.Holding.create({
          symbol: bp.symbol,
          company_name: bp.symbol,
          shares: bp.shares,
          avg_price: bp.avg_price,
          current_price: bp.current_price,
          sector: '',
          day_change_percent: 0,
        });
        created.push(h);
      } else if (existing.shares !== bp.shares) {
        await sr.entities.Holding.update(existing.id, { shares: bp.shares, avg_price: bp.avg_price, current_price: bp.current_price });
        updated.push({ symbol: sym, from: existing.shares, to: bp.shares });
      }
    }
    // App holdings no longer at the broker: record a reconciliation sell trade
    // (with realized P&L + provenance) BEFORE removing, so the ledger keeps
    // transaction history for externally-closed positions.
    for (const [sym, h] of Object.entries(appMap)) {
      if (!brokerMap[sym]) {
        const exitPrice = h.current_price || h.avg_price;
        const realizedPnl = (exitPrice - h.avg_price) * h.shares;
        await sr.entities.Trade.create({
          symbol: h.symbol,
          company_name: h.company_name || h.symbol,
          action: 'sell',
          shares: h.shares,
          price: exitPrice,
          total_value: h.shares * exitPrice,
          notes: 'Reconciliation: position no longer held at broker (externally closed)',
          ai_recommended: false,
          order_status: 'reconciled_external',
          source: 'reconciliation',
          realized_pnl: realizedPnl,
        });
        await sr.entities.Holding.delete(h.id);
        removed.push(sym);
      }
    }

    return Response.json({
      ok: true,
      broker_positions: brokerPositions.length,
      created: created.length,
      updated: updated.length,
      removed,
      details: { created, updated },
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}