import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { fetchQuotes } from '../../shared/marketDataAdapter.ts';
import { usMarketSession, isUsMarketOpen } from '../../shared/marketHours.ts';

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
    const assetClassBySym = {};
    holdings.forEach((h) => { assetClassBySym[String(h.symbol).toUpperCase()] = h.asset_class || 'stocks'; });
    decisions.forEach((d) => { if (!assetClassBySym[String(d.symbol).toUpperCase()]) assetClassBySym[String(d.symbol).toUpperCase()] = d.asset_class || 'stocks'; });
    assetClassBySym['SPY'] = 'stocks';
    holdings.forEach((h) => openSyms.add(String(h.symbol).toUpperCase()));
    openSyms.add('SPY');

    const symbols = [...openSyms].slice(0, 40);
    const items = symbols.map((s) => ({ symbol: s, asset_class: assetClassBySym[s] || 'stocks' }));
    const now = new Date().toISOString();
    const session = usMarketSession();
    const marketOpen = isUsMarketOpen();
    const quotes = await fetchQuotes(items, key);
    let captured = 0;
    await Promise.all(quotes.filter((q) => !q.error && q.price > 0).map(async (q) => {
      try {
        // Record the provider's authoritative observation time, not the database
        // insertion time. An after-hours close fetched at 9pm must not appear fresh.
        const providerTs = q.quote_timestamp
          ? new Date(q.quote_timestamp * 1000).toISOString()
          : now;
        await sr.entities.PriceSnapshot.create({
          symbol: q.symbol, price: q.price, timestamp: now,
          provider_timestamp: providerTs,
          market_session: session,
          is_market_open: marketOpen,
          source: q.asset_class === 'crypto' ? 'binance' : 'finnhub',
        });
        captured++;
      } catch (e) { /* skip */ }
    }));

    return Response.json({ ok: true, captured, symbols: symbols.length });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}