import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';

const FINNHUB_QUOTE = 'https://finnhub.io/api/v1/quote';

// Scheduled price-snapshot capture. Records a point-in-time price for every
// symbol that has an open AI buy decision (plus current holdings + SPY benchmark),
// so the outcome-labeling engine can compute forward returns and excursions.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const key = secrets.get('FINNHUB_API_KEY');
    if (!key) return Response.json({ error: 'FINNHUB_API_KEY not set' }, { status: 500 });

    const sr = base44.asServiceRole;
    const decisions = await sr.entities.AITradeDecision.list('-created_date', 200);
    const fills = await sr.entities.Fill.list('-created_date', 5000);
    const openSyms = new Set();
    decisions.filter((d) => d.action === 'buy' && d.status === 'executed').forEach((d) => {
      const sym = String(d.symbol).toUpperCase();
      const hasSell = fills.some(
        (f) => String(f.symbol).toUpperCase() === sym && f.side === 'sell' && new Date(f.timestamp) >= new Date(d.created_date)
      );
      if (!hasSell) openSyms.add(sym);
    });
    const holdings = await sr.entities.Holding.list();
    holdings.forEach((h) => openSyms.add(String(h.symbol).toUpperCase()));
    openSyms.add('SPY');

    const symbols = [...openSyms].slice(0, 40);
    const now = new Date().toISOString();
    let captured = 0;
    await Promise.all(symbols.map(async (symbol) => {
      try {
        const res = await fetch(`${FINNHUB_QUOTE}?symbol=${encodeURIComponent(symbol)}&token=${key}`);
        const d = await res.json();
        if (!d || typeof d.c !== 'number' || d.c === 0) return;
        await sr.entities.PriceSnapshot.create({ symbol, price: d.c, timestamp: now, source: 'finnhub' });
        captured++;
      } catch (e) { /* skip individual failures */ }
    }));

    return Response.json({ ok: true, captured, symbols: symbols.length });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}