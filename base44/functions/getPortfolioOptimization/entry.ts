import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { loadAlignedPortfolioData } from '../../shared/portfolioData.ts';
import {
  mean,
  covarianceMatrix,
  portfolioVariance,
  portfolioReturn,
  maxSharpeWeightsLongOnly,
  efficientFrontier,
} from '../../shared/riskAnalytics.ts';

// Markowitz portfolio optimization: computes the efficient frontier, the max Sharpe
// (tangency) portfolio, and compares current vs optimal weights.
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

    const { assetReturns, availableSymbols, weights, observations } = aligned;

    if (availableSymbols.length < 2) {
      return Response.json({ error: 'At least 2 holdings required for optimization' }, { status: 400 });
    }

    // Annualized expected returns and covariance matrix
    const mu = assetReturns.map((r) => mean(r) * 252);
    const cov = covarianceMatrix(assetReturns);
    if (!cov) return Response.json({ error: 'Failed to compute covariance' }, { status: 500 });
    const covAnnual = cov.map((row) => row.map((x) => x * 252));

    // Current portfolio metrics
    const currentReturn = portfolioReturn(weights, mu);
    const currentVol = Math.sqrt(portfolioVariance(weights, covAnnual));
    const currentSharpe = currentVol > 0 ? currentReturn / currentVol : 0;

    // Optimal (max Sharpe, long-only) portfolio
    const optimalWeights = maxSharpeWeightsLongOnly(mu, covAnnual);
    let optimal = null;
    if (optimalWeights) {
      const optReturn = portfolioReturn(optimalWeights, mu);
      const optVol = Math.sqrt(portfolioVariance(optimalWeights, covAnnual));
      optimal = {
        weights: optimalWeights,
        expectedReturn: optReturn,
        volatility: optVol,
        sharpe: optVol > 0 ? optReturn / optVol : 0,
      };
    }

    // Efficient frontier (downsampled for chart)
    const frontier = efficientFrontier(mu, covAnnual, 40);
    const frontierPoints = frontier || [];

    // Weight comparison per symbol
    const weightComparison = availableSymbols.map((s, i) => ({
      symbol: s,
      current: Math.round(weights[i] * 1000) / 10,
      optimal: optimalWeights ? Math.round(optimalWeights[i] * 1000) / 10 : 0,
    }));

    return Response.json({
      ok: true,
      observations,
      current: {
        weights,
        expectedReturn: currentReturn,
        volatility: currentVol,
        sharpe: currentSharpe,
      },
      optimal,
      efficientFrontier: frontierPoints,
      weightComparison,
      symbols: availableSymbols,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}