import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { fetchQuotes } from '../../shared/marketDataAdapter.ts';

// Multi-asset quote endpoint. Accepts a list of { symbol, asset_class } and
// returns live prices routed by asset class (stocks → Finnhub, crypto → Binance).
// Used by the frontend to price any holding regardless of asset class.
export default async function (req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const items = Array.isArray(body.items) ? body.items : Array.isArray(body.symbols) ? body.symbols.map((s) => ({ symbol: s })) : [];
    if (!items.length) return Response.json({ error: 'Provide items: [{ symbol, asset_class }]' }, { status: 400 });
    if (items.length > 40) return Response.json({ error: 'Max 40 symbols per batch' }, { status: 400 });

    const key = secrets.get('FINNHUB_API_KEY');
    const quotes = await fetchQuotes(items, key);

    return Response.json({ ok: true, quotes });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}