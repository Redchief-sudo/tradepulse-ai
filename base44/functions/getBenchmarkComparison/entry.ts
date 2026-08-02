import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { loadAlignedPortfolioData } from '../../shared/portfolioData.ts';
import { portfolioDailyReturns, computeBenchmarkComparison } from '../../shared/riskAnalytics.ts';

const TIMEFRAMES = [
  { key: '1M', days: 21 },
  { key: '3M', days: 63 },
  { key: '6M', days: 126 },
  { key: '1Y', days: 252 },
];

// Compare portfolio performance against the S&P 500 (SPY) benchmark across
// multiple timeframes. Returns rebased equity curves + alpha, tracking error,
// and information ratio per timeframe.
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

    const { commonDates, benchReturns, assetReturns, weights, benchmark, observations } = aligned;
    const portReturns = portfolioDailyReturns(assetReturns, weights);

    // Full-period comparison for the equity curve chart
    const fullComparison = computeBenchmarkComparison(portReturns, benchReturns);
    if (!fullComparison) return Response.json({ error: 'Insufficient data' }, { status: 502 });

    // Chart data: rebased equity curves paired with dates
    const chartData = commonDates.map((d, i) => ({
      date: d,
      portfolio: Math.round(fullComparison.portEquity[i] * 100) / 100,
      benchmark: Math.round(fullComparison.benchEquity[i] * 100) / 100,
    }));

    // Per-timeframe metrics
    const timeframes = {};
    for (const tf of TIMEFRAMES) {
      const sliceStart = Math.max(0, portReturns.length - tf.days);
      const slicedPort = portReturns.slice(sliceStart);
      const slicedBench = benchReturns.slice(sliceStart);
      const tfComparison = computeBenchmarkComparison(slicedPort, slicedBench);
      if (tfComparison) {
        timeframes[tf.key] = {
          portReturn: tfComparison.portReturn,
          benchReturn: tfComparison.benchReturn,
          alpha: tfComparison.alpha,
          trackingError: tfComparison.trackingError,
          informationRatio: tfComparison.informationRatio,
        };
      }
    }

    return Response.json({
      ok: true,
      benchmark,
      observations,
      chartData,
      timeframes,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}