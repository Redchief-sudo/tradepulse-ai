// Real market data client.
// Crypto: LIVE via Binance public API (no key, CORS-enabled, real OHLCV).
// Stocks: requires a Builder+ backend function with a market-data provider key (Polygon/Finnhub).

const BINANCE = 'https://api.binance.com';

export async function fetchCryptoCandles(symbol = 'BTCUSDT', interval = '1d', limit = 200) {
  const res = await fetch(
    `${BINANCE}/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`
  );
  if (!res.ok) throw new Error(`Binance klines ${res.status}`);
  const raw = await res.json();
  return raw.map((k) => ({
    time: k[0],
    open: +k[1],
    high: +k[2],
    low: +k[3],
    close: +k[4],
    volume: +k[5],
    closeTime: k[6],
  }));
}

export async function fetchCryptoQuote(symbol = 'BTCUSDT') {
  const res = await fetch(`${BINANCE}/api/v3/ticker/24hr?symbol=${symbol}`);
  if (!res.ok) throw new Error(`Binance ticker ${res.status}`);
  const d = await res.json();
  return {
    symbol,
    price: +d.lastPrice,
    changePercent: +d.priceChangePercent,
    high: +d.highPrice,
    low: +d.lowPrice,
    volume: +d.volume,
  };
}

// Fetch candles for the whole crypto universe in parallel (real data).
export async function fetchCryptoUniverse(symbols) {
  const list = symbols || CRYPTO_SYMBOLS.map((s) => s.symbol);
  const results = await Promise.allSettled(
    list.map(async (sym) => {
      const [candles, quote] = await Promise.all([
        fetchCryptoCandles(sym, '1d', 200),
        fetchCryptoQuote(sym),
      ]);
      return { symbol: sym, candles, quote };
    })
  );
  const fulfilled = results
    .filter((r) => r.status === 'fulfilled')
    .map((r) => r.value);
  if (fulfilled.length === 0) {
    const reasons = [...new Set(results
      .filter((r) => r.status === 'rejected')
      .map((r) => r.reason?.message || String(r.reason))
    )];
    throw new Error(`No Binance market data returned: ${reasons.join('; ') || 'all requests failed'}`);
  }
  return fulfilled;
}

export const CRYPTO_SYMBOLS = [
  { symbol: 'BTCUSDT', label: 'Bitcoin', ticker: 'BTC' },
  { symbol: 'ETHUSDT', label: 'Ethereum', ticker: 'ETH' },
  { symbol: 'SOLUSDT', label: 'Solana', ticker: 'SOL' },
  { symbol: 'BNBUSDT', label: 'BNB', ticker: 'BNB' },
  { symbol: 'XRPUSDT', label: 'XRP', ticker: 'XRP' },
  { symbol: 'AVAXUSDT', label: 'Avalanche', ticker: 'AVAX' },
  { symbol: 'DOGEUSDT', label: 'Dogecoin', ticker: 'DOGE' },
  { symbol: 'LINKUSDT', label: 'Chainlink', ticker: 'LINK' },
];

// Stock data — real implementation requires Builder+ backend function + provider key.
export async function fetchStockCandles(symbol) {
  throw new Error(
    `Stock data for ${symbol} requires a Builder+ backend function with a market-data API key (e.g. Polygon.io). Crypto is live now via Binance.`
  );
}
