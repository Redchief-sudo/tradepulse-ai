import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { runBacktest, walkForward, getStrategy } from '../../shared/backtest.ts';
import { fetchCandles } from '../../shared/marketDataServer.ts';

// Run a deterministic backtest or walk-forward analysis for a strategy + symbol.
// Body: { strategy, symbol, from, to, initialCapital?, assetClass?, walk_forward?, trainSize?, testSize? }
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const strategy = getStrategy(body.strategy);
    if (!strategy) return Response.json({ error: `Unknown strategy: ${body.strategy}` }, { status: 400 });
    const symbol = String(body.symbol || 'AAPL').toUpperCase();

    const to = body.to ? Math.floor(new Date(body.to).getTime() / 1000) : Math.floor(Date.now() / 1000);
    const from = body.from ? Math.floor(new Date(body.from).getTime() / 1000) : to - 730 * 86400; // default 2 years
    const series = await fetchCandles(symbol, from, to);
    if (!series) return Response.json({ error: 'No candle data available for this symbol/range.' }, { status: 502 });

    const config = {
      initialCapital: Number(body.initialCapital) || 10000,
      assetClass: body.assetClass || 'stocks',
      params: body.params || null,
    };

    if (body.walk_forward) {
      config.trainSize = Number(body.trainSize) || 252;
      config.testSize = Number(body.testSize) || 63;
      config.step = Number(body.step) || config.testSize;
      const wf = walkForward(series, strategy, config);
      return Response.json({ ok: true, symbol, strategy: strategy.name, mode: 'walk_forward', ...wf });
    }

    const result = runBacktest(series, strategy, config);
    // Downsample the equity curve to ~120 points for the frontend chart.
    const eq = result.equityCurve;
    const stride = Math.max(1, Math.floor(eq.length / 120));
    const downsampled = eq.filter((_, i) => i % stride === 0);
    if (eq.length && downsampled[downsampled.length - 1] !== eq[eq.length - 1]) downsampled.push(eq[eq.length - 1]);

    return Response.json({
      ok: true,
      symbol,
      strategy: strategy.name,
      mode: 'backtest',
      bars: series.length,
      metrics: result.metrics,
      equity_curve: downsampled,
      trades: result.trades,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}