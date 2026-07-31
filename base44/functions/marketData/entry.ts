import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';

// Finnhub quote endpoint returns { c: current, d: change, dp: percent change, h, l, o, pc }
// Finnhub profile2 endpoint returns company fundamentals: name, finnhubIndustry, marketCapitalization, peNormalized, etc.
const FINNHUB_QUOTE = 'https://finnhub.io/api/v1/quote';
const FINNHUB_PROFILE = 'https://finnhub.io/api/v1/stock/profile2';

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const symbols = Array.isArray(body.symbols) ? body.symbols : [];
    const includeFundamentals = !!body.include_fundamentals;
    if (symbols.length === 0) return Response.json({ quotes: [] });

    const key = secrets.get('FINNHUB_API_KEY');
    if (!key) return Response.json({ error: 'FINNHUB_API_KEY not set' }, { status: 500 });

    const batch = symbols.slice(0, 40).map((s) => String(s).toUpperCase().trim()).filter(Boolean);

    const results = await Promise.all(
      batch.map(async (symbol) => {
        try {
          const res = await fetch(`${FINNHUB_QUOTE}?symbol=${encodeURIComponent(symbol)}&token=${key}`);
          if (!res.ok) return { symbol, error: `HTTP ${res.status}` };
          const data = await res.json();
          if (!data || typeof data.c !== 'number' || data.c === 0) {
            return { symbol, error: 'no data' };
          }
          const quote = {
            symbol,
            current_price: data.c,
            day_change_percent: typeof data.dp === 'number' ? data.dp : 0,
            day_change: typeof data.d === 'number' ? data.d : 0,
            high: data.h,
            low: data.l,
            open: data.o,
            prev_close: data.pc,
          };
          if (includeFundamentals) {
            try {
              const pres = await fetch(`${FINNHUB_PROFILE}?symbol=${encodeURIComponent(symbol)}&token=${key}`);
              if (pres.ok) {
                const p = await pres.json();
                quote.fundamentals = {
                  name: p.name || null,
                  industry: p.finnhubIndustry || null,
                  market_cap: p.marketCapitalization || null,
                  pe_ratio: p.peNormalized || p.pe || null,
                  shares_outstanding: p.shareOutstanding || null,
                  logo: p.logo || null,
                };
              }
            } catch (e) {
              /* fundamentals optional */
            }
          }
          return quote;
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