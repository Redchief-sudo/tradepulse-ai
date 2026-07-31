import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';

// Finnhub quote endpoint returns { c: current, d: change, dp: percent change, h, l, o, pc }
// Free tier: 60 calls/min — we batch with Promise.all but cap to stay safe.
const FINNHUB_QUOTE = 'https://finnhub.io/api/v1/quote';

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const symbols = Array.isArray(body.symbols) ? body.symbols : [];
    if (symbols.length === 0) return Response.json({ quotes: [] });

    const key = secrets.get('FINNHUB_API_KEY');
    if (!key) return Response.json({ error: 'FINNHUB_API_KEY not set' }, { status: 500 });

    // Cap to 40 symbols per call to stay within free-tier rate limits.
    const batch = symbols.slice(0, 40).map((s) => String(s).toUpperCase().trim()).filter(Boolean);

    const results = await Promise.all(
      batch.map(async (symbol) => {
        try {
          const res = await fetch(`${FINNHUB_QUOTE}?symbol=${encodeURIComponent(symbol)}&token=${key}`);
          if (!res.ok) return { symbol, error: `HTTP ${res.status}` };
          const data = await res.json();
          // Finnhub returns c:0 for invalid/closed symbols — treat 0 current as no data.
          if (!data || typeof data.c !== 'number' || data.c === 0) {
            return { symbol, error: 'no data' };
          }
          return {
            symbol,
            current_price: data.c,
            day_change_percent: typeof data.dp === 'number' ? data.dp : 0,
            day_change: typeof data.d === 'number' ? data.d : 0,
            high: data.h,
            low: data.l,
            open: data.o,
            prev_close: data.pc,
          };
        } catch (e) {
          return { symbol, error: e.message };
        }
      })
    );

    return Response.json({ quotes: results });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}