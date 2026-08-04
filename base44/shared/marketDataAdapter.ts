// Unified multi-asset market-data adapter.
//
// Routes quote + candle requests by asset class:
//   stocks  → Finnhub (quote) + Yahoo Finance (daily candles)
//   crypto  → Binance public API (24h ticker + klines) — no key required
//
// This is the single data-fetching boundary every backend function should use,
// so the stop-loss cycle, snapshot capture, and execution gateway all agree on
// how to price a position regardless of asset class.

const FINNHUB_QUOTE = 'https://finnhub.io/api/v1/quote';
const YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart';
const COINBASE_STATS = 'https://api.exchange.coinbase.com/products';
const COINBASE_SPOT = 'https://api.coinbase.com/v2/prices';

export interface Quote {
  symbol: string;
  asset_class: string;
  price: number;
  day_change_percent: number;
  quote_timestamp?: number; // unix seconds — provider's authoritative observation time
  error?: string;
}

export interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

function safeNum(n: any): number {
  const v = Number(n);
  return Number.isFinite(v) ? v : 0;
}

// Normalize a crypto symbol to Coinbase's BASE-USD convention (e.g. BTC → BTC-USD).
// Accepts bare bases (BTC), BASE-USD, or BASEUSDT — strips the quote and re-pairs to USD.
function coinbaseProduct(symbol: string): string {
  const s = String(symbol).toUpperCase().replace(/[^A-Z0-9]/g, '');
  const base = s.replace(/(USDT|USDC|BUSD|USD)$/i, '') || s;
  return `${base}-USD`;
}

// Fetch a single quote for any asset class.
export async function fetchQuote(symbol: string, assetClass: string, finnhubKey?: string): Promise<Quote> {
  const sym = String(symbol).toUpperCase();
  const ac = (assetClass || 'stocks').toLowerCase();

  if (ac === 'crypto') {
    try {
      const product = coinbaseProduct(sym);
      const res = await fetch(`${COINBASE_STATS}/${encodeURIComponent(product)}/stats`);
      if (!res.ok) return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: `Coinbase HTTP ${res.status}` };
      const d = await res.json();
      const last = safeNum(d.last);
      const open = safeNum(d.open);
      if (last <= 0) return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: 'no price' };
      const dayChange = open > 0 ? ((last - open) / open) * 100 : 0;
      return { symbol: sym, asset_class: ac, price: last, day_change_percent: dayChange, quote_timestamp: Math.floor(Date.now() / 1000) };
    } catch (e: any) {
      return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: e.message };
    }
  }

  // stocks (and other equity-like classes fall back to Finnhub)
  try {
    if (!finnhubKey) return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: 'no finnhub key' };
    const res = await fetch(`${FINNHUB_QUOTE}?symbol=${encodeURIComponent(sym)}&token=${finnhubKey}`);
    if (!res.ok) return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: `Finnhub HTTP ${res.status}` };
    const d = await res.json();
    if (!d || typeof d.c !== 'number' || d.c === 0) return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: 'no data' };
    return { symbol: sym, asset_class: ac, price: d.c, day_change_percent: typeof d.dp === 'number' ? d.dp : 0, quote_timestamp: typeof d.t === 'number' && d.t > 0 ? d.t : Math.floor(Date.now() / 1000) };
  } catch (e: any) {
    return { symbol: sym, asset_class: ac, price: 0, day_change_percent: 0, error: e.message };
  }
}

// Batch quote fetch — items: [{ symbol, asset_class }].
export async function fetchQuotes(items: { symbol: string; asset_class?: string }[], finnhubKey?: string): Promise<Quote[]> {
  return Promise.all(items.map((it) => fetchQuote(it.symbol, it.asset_class || 'stocks', finnhubKey)));
}

// Fetch daily OHLCV candles for any asset class.
// `from`/`to` are unix seconds.
export async function fetchCandles(symbol: string, assetClass: string, from: number, to: number, finnhubKey?: string): Promise<Candle[] | null> {
  const sym = String(symbol).toUpperCase();
  const ac = (assetClass || 'stocks').toLowerCase();

  if (ac === 'crypto') {
    try {
      const product = coinbaseProduct(sym);
      const startIso = new Date(from * 1000).toISOString();
      const endIso = new Date(to * 1000).toISOString();
      const res = await fetch(`${COINBASE_STATS}/${encodeURIComponent(product)}/candles?granularity=86400&start=${startIso}&end=${endIso}`);
      if (!res.ok) return null;
      const rows = await res.json();
      // Coinbase candle format: [time, low, high, open, close, volume] (oldest-first or newest-first varies)
      if (!Array.isArray(rows) || rows.length < 30) return null;
      const candles = rows.map((k: any[]) => ({
        date: new Date(Number(k[0]) * 1000).toISOString().slice(0, 10),
        open: safeNum(k[3]),
        high: safeNum(k[2]),
        low: safeNum(k[1]),
        close: safeNum(k[4]),
        volume: safeNum(k[5]),
      })).filter((c: Candle) => c.close > 0);
      // Ensure chronological order
      candles.sort((a, b) => a.date.localeCompare(b.date));
      return candles.length >= 30 ? candles : null;
    } catch (e) {
      return null;
    }
  }

  // stocks → Yahoo Finance (no key needed; Finnhub free tier has no historical candles)
  try {
    const url = `${YAHOO_CHART}/${encodeURIComponent(sym)}?period1=${from}&period2=${to}&interval=1d`;
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!res.ok) return null;
    const j = await res.json();
    const result = j?.chart?.result?.[0];
    const ts = result?.timestamp;
    const quote = result?.indicators?.quote?.[0];
    if (!ts || !quote || !quote.close || ts.length < 30) return null;
    const candles: Candle[] = [];
    for (let i = 0; i < ts.length; i++) {
      const c = quote.close[i];
      if (c == null || c <= 0) continue;
      candles.push({
        date: new Date(ts[i] * 1000).toISOString().slice(0, 10),
        open: quote.open[i] ?? c,
        high: quote.high[i] ?? c,
        low: quote.low[i] ?? c,
        close: c,
        volume: quote.volume[i] ?? 0,
      });
    }
    return candles.length >= 30 ? candles : null;
  } catch (e) {
    return null;
  }
}