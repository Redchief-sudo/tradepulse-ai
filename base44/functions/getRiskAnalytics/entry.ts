import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { fetchCandles } from '../../shared/marketDataServer.ts';
import { std, beta, computePortfolioRisk, computeCorrelationMatrix } from '../../shared/riskAnalytics.ts';

const BENCHMARK = 'SPY';
const LOOKBACK_DAYS = 252; // 1 year of trading days

// Compute deterministic portfolio risk metrics (volatility, VaR, beta, Sharpe,
// max drawdown, correlation matrix) from 1 year of historical daily candles.
// No secrets required — Yahoo Finance's free chart endpoint is used.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const holdings = await base44.entities.Holding.list();
    if (!holdings || holdings.length === 0) {
      return Response.json({ error: 'No holdings to analyze' }, { status: 400 });
    }

    const to = Math.floor(Date.now() / 1000);
    const from = to - (LOOKBACK_DAYS + 30) * 86400; // extra buffer for return calc

    // Fetch benchmark + all holding candles in parallel
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

    if (!symbolReturnMap[BENCHMARK]) {
      return Response.json({ error: 'Benchmark (SPY) data unavailable' }, { status: 502 });
    }

    // Find common dates across all available symbols + benchmark
    const availableSymbols = symbols.filter((s) => symbolReturnMap[s]);
    if (availableSymbols.length === 0) {
      return Response.json({ error: 'No historical data available for holdings' }, { status: 502 });
    }

    const allDateSets = [BENCHMARK, ...availableSymbols].map((s) => new Set(symbolReturnMap[s].keys()));
    let commonDates = [...allDateSets[0]];
    for (let i = 1; i < allDateSets.length; i++) {
      commonDates = commonDates.filter((d) => allDateSets[i].has(d));
    }

    if (commonDates.length < 30) {
      return Response.json({ error: 'Insufficient overlapping data for risk analysis' }, { status: 502 });
    }

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

    // Individual holding risk metrics
    const individual = availableSymbols.map((s, i) => ({
      symbol: s,
      weight: weights[i],
      annualVol: std(assetReturns[i]) * Math.sqrt(252),
      beta: beta(assetReturns[i], benchReturns),
    }));

    // Portfolio-level risk metrics
    const portfolioMetrics = computePortfolioRisk(assetReturns, weights, benchReturns);

    // Pairwise correlation matrix
    const corrMatrix = computeCorrelationMatrix(assetReturns, availableSymbols);

    return Response.json({
      ok: true,
      benchmark: BENCHMARK,
      observations: commonDates.length,
      portfolio: portfolioMetrics,
      individual,
      correlation: corrMatrix,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}