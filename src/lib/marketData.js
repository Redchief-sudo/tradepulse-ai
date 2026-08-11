// Real market data client. All provider traffic goes through the authenticated
// backend boundary so browser CORS and regional provider restrictions cannot
// silently turn a live feed into an empty result.
import { base44 } from '@/api/base44Client';

export async function fetchCryptoCandles(symbol = 'BTCUSDT', interval = '1d', limit = 200) {
  if (interval !== '1d') throw new Error(`Unsupported candle interval: ${interval}`);
  const result = await base44.functions.invoke('getMultiAssetQuotes', {
    items: [{ symbol, asset_class: 'crypto' }],
    include_candles: true,
  });
  const series = (result.data || result)?.series?.[0];
  if (!series?.candles?.length) throw new Error(series?.error || `No Coinbase candles returned for ${symbol}`);
  return series.candles.slice(-limit);
}

export async function fetchCryptoQuote(symbol = 'BTCUSDT') {
  const result = await base44.functions.invoke('getMultiAssetQuotes', {
    items: [{ symbol, asset_class: 'crypto' }],
  });
  const d = (result.data || result)?.quotes?.[0];
  if (!d || d.error || !d.price) throw new Error(d?.error || `No Coinbase quote returned for ${symbol}`);
  return {
    symbol,
    price: Number(d.price),
    changePercent: Number(d.day_change_percent) || 0,
    bid: Number(d.bid) || null,
    ask: Number(d.ask) || null,
    volume: Number(d.volume) || 0,
    source: d.source,
  };
}

// Fetch candles for the whole crypto universe in parallel (real data).
export async function fetchCryptoUniverse(symbols) {
  const list = symbols || CRYPTO_SYMBOLS.map((s) => s.symbol);
  const result = await base44.functions.invoke('getMultiAssetQuotes', {
    items: list.map((symbol) => ({ symbol, asset_class: 'crypto' })),
    include_candles: true,
  });
  const payload = result.data || result;
  const quoteBySymbol = new Map((payload.quotes || []).map((quote) => [String(quote.symbol).toUpperCase(), quote]));
  const seriesBySymbol = new Map((payload.series || []).map((entry) => [String(entry.symbol).toUpperCase(), entry]));
  const fulfilled = list.flatMap((symbol) => {
    const quote = quoteBySymbol.get(String(symbol).toUpperCase());
    const series = seriesBySymbol.get(String(symbol).toUpperCase());
    if (!quote || quote.error || !series?.candles?.length) return [];
    return [{
      symbol,
      candles: series.candles,
      quote: {
        symbol,
        price: Number(quote.price),
        changePercent: Number(quote.day_change_percent) || 0,
        bid: Number(quote.bid) || null,
        ask: Number(quote.ask) || null,
        volume: Number(quote.volume) || 0,
        source: quote.source,
      },
    }];
  });
  if (fulfilled.length === 0) {
    const reasons = [...new Set([
      ...(payload.quotes || []).map((quote) => quote.error).filter(Boolean),
      ...(payload.series || []).map((entry) => entry.error).filter(Boolean),
    ])];
    throw new Error(`No Coinbase market data returned: ${reasons.join('; ') || 'all provider requests failed'}`);
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
    `Stock data for ${symbol} is available only through the authenticated backend market-data route. Crypto is routed through Coinbase.`
  );
}
