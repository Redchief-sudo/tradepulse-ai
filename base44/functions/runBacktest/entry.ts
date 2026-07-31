import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { runBacktest, walkForward, getStrategy } from '../../shared/backtest.ts';

const YAHOO_CHART = 'https://query1.finance.yahoo.com/v8/finance/chart';

// Fetch daily OHLCV candles. Uses Yahoo Finance's free chart endpoint (no API key
// required) — Finnhub's free tier does not serve historical candles, and Stooq now
// blocks server-side fetches with a JS challenge.
async function fetchCandles(symbol, from, to) {
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