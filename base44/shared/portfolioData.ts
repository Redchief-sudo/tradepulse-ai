// Shared portfolio data loading: fetches historical candles for all holdings + benchmark,
// computes daily returns, aligns by common dates, and computes portfolio weights.
// Used by both getRiskAnalytics and getBenchmarkComparison to avoid duplication.

import { fetchCandles } from './marketDataServer.ts';

const BENCHMARK = 'SPY';
const LOOKBACK_DAYS = 252;

export async function loadAlignedPortfolioData(holdings) {
  const to = Math.floor(Date.now() / 1000);
  const from = to - (LOOKBACK_DAYS + 30) * 86400;

  const symbols = [...new Set(holdings.map((h) => h.symbol.toUpperCase()))];
  const allSymbols = [BENCHMARK, ...symbols];
  const candleResults = await Promise.all(
    allSymbols.map((s) => fetchCandles(s, from, to).then((c) => ({ symbol: s, candles: c })))
  );

  // Build date -> daily return map for each symbol
  const symbolReturnMap = {};
  for (const { symbol, candles } of candleResults) {
    if (!candles) continue;
    const returns = new Map();
    for (let i = 1; i < candles.length; i++) {
      const prev = candles[i - 1].close;
      const curr = candles[i].close;
      if (prev > 0) returns.set(candles[i].date, (curr - prev) / prev);
    }
    symbolReturnMap[symbol] = returns;
  }

  if (!symbolReturnMap[BENCHMARK]) return { error: 'Benchmark (SPY) data unavailable' };

  const availableSymbols = symbols.filter((s) => symbolReturnMap[s]);
  if (availableSymbols.length === 0) return { error: 'No historical data available for holdings' };

  // Find common dates across all available symbols + benchmark
  const allDateSets = [BENCHMARK, ...availableSymbols].map((s) => new Set(symbolReturnMap[s].keys()));
  let commonDates = [...allDateSets[0]];
  for (let i = 1; i < allDateSets.length; i++) {
    commonDates = commonDates.filter((d) => allDateSets[i].has(d));
  }

  if (commonDates.length < 30) return { error: 'Insufficient overlapping data for analysis' };

  // Build aligned return arrays
  const benchReturns = commonDates.map((d) => symbolReturnMap[BENCHMARK].get(d));
  const assetReturns = availableSymbols.map((s) => commonDates.map((d) => symbolReturnMap[s].get(d)));

  // Compute portfolio weights by market value
  const holdingsBySymbol = {};
  for (const h of holdings) {
    const sym = h.symbol.toUpperCase();
    if (!holdingsBySymbol[sym]) holdingsBySymbol[sym] = { shares: 0, price: 0 };
    holdingsBySymbol[sym].shares += h.shares;
    holdingsBySymbol[sym].price = h.current_price || h.avg_price;
  }
  const totalValue = availableSymbols.reduce((sum, s) => {
    const hd = holdingsBySymbol[s];
    return sum + hd.shares * hd.price;
  }, 0);
  const weights = availableSymbols.map((s) => {
    const hd = holdingsBySymbol[s];
    return totalValue > 0 ? (hd.shares * hd.price) / totalValue : 0;
  });

  return {
    benchmark: BENCHMARK,
    commonDates,
    benchReturns,
    assetReturns,
    availableSymbols,
    weights,
    observations: commonDates.length,
  };
}