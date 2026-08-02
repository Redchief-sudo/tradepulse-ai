// Server-side historical candle fetching. Uses Yahoo Finance's free chart endpoint
// (no API key required) — Finnhub's free tier does not serve historical candles,
// and Stooq now blocks server-side fetches with a JS challenge.
// Extracted here so both runBacktest and getRiskAnalytics share a single implementation.

const YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart';

export async function fetchCandles(symbol, from, to) {
  const url = `${YAHOO_CHART}/${encodeURIComponent(symbol)}?period1=${from}&period2=${to}&interval=1d`;
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
  if (!res.ok) return null;
  const j = await res.json();
  const result = j?.chart?.result?.[0];
  const ts = result?.timestamp;
  const quote = result?.indicators?.quote?.[0];
  if (!ts || !quote || !quote.close || ts.length < 30) return null;
  const rows = [];
  for (let i = 0; i < ts.length; i++) {
    const c = quote.close[i];
    if (c == null || c <= 0) continue; // skip null bars (holidays / early closes)
    rows.push({
      date: new Date(ts[i] * 1000).toISOString().slice(0, 10),
      open: quote.open[i] ?? c,
      high: quote.high[i] ?? c,
      low: quote.low[i] ?? c,
      close: c,
      volume: quote.volume[i] ?? 0,
    });
  }
  return rows.length >= 30 ? rows : null;
}