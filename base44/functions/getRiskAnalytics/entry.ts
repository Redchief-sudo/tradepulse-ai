import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { loadAlignedPortfolioData } from '../../shared/portfolioData.ts';
import { std, beta, computePortfolioRisk, computeCorrelationMatrix } from '../../shared/riskAnalytics.ts';

// Compute deterministic portfolio risk metrics (volatility, VaR, beta, Sharpe,
// max drawdown, correlation matrix) from 1 year of historical daily candles.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const holdings = await base44.entities.Holding.list();
    if (!holdings || holdings.length === 0) {
      return Response.json({ error: 'No holdings to analyze' }, { status: 400 });
    }

    const aligned = await loadAlignedPortfolioData(holdings);
    if (aligned.error) return Response.json({ error: aligned.error }, { status: 502 });

    const { benchReturns, assetReturns, availableSymbols, weights, benchmark, observations } = aligned;

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
      benchmark,
      observations,
      portfolio: portfolioMetrics,
      individual,
      correlation: corrMatrix,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}