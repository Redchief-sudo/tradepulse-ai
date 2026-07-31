// Deterministic market-regime classifier — computed from the SPY PriceSnapshot
// time series, NOT LLM estimation. Classifies the broad market into one of five
// regimes and produces a regime-aware position-sizing multiplier that the risk
// engine and autonomous scan apply to every new buy.

const REGIME_STRATEGY = {
  low_vol_bull: 'Trend-following — full position sizing, favor momentum breakouts',
  high_vol_bear: 'Defensive / capital preservation — reduce sizing, raise cash, tighten stops',
  range_bound_choppy: 'Mean-reversion — fade extremes, smaller sizes, tight stops',
  liquidity_crisis: 'De-risk / capital preservation — block new buys, exit weak positions',
  transition: 'Adaptive / cautious — moderate sizing, wait for confirmation',
};

// Fraction of the strategy's max position size allowed in each regime.
const REGIME_POSITION_MULTIPLIER = {
  low_vol_bull: 1.0,
  high_vol_bear: 0.5,
  range_bound_choppy: 0.7,
  liquidity_crisis: 0.0,
  transition: 0.75,
};

function std(arr) {
  if (arr.length < 2) return 0;
  const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
  const variance = arr.reduce((a, b) => a + (b - mean) ** 2, 0) / (arr.length - 1);
  return Math.sqrt(variance);
}

function rsi(closes, period = 14) {
  if (closes.length < period + 1) return 50;
  let gains = 0, losses = 0;
  for (let i = closes.length - period; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff >= 0) gains += diff; else losses -= diff;
  }
  const avgGain = gains / period;
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

// classifyRegime — deterministic from an oldest-first close-price series.
export function classifyRegime(closes) {
  if (!closes || closes.length < 10) {
    return {
      market_regime: 'transition', regime_confidence: 30,
      regime_strategy: REGIME_STRATEGY.transition,
      position_multiplier: REGIME_POSITION_MULTIPLIER.transition,
      realized_vol: null, rsi: null, trend: null,
    };
  }
  const series = closes.slice(-Math.min(closes.length, 288)); // up to ~1 day of 5-min bars
  const returns = [];
  for (let i = 1; i < series.length; i++) {
    if (series[i - 1] > 0) returns.push((series[i] - series[i - 1]) / series[i - 1]);
  }
  const realizedVol = std(returns) * Math.sqrt(252 * 78); // annualized (78 5-min bars/day)
  const rsiVal = rsi(series, Math.min(14, series.length - 1));

  // Trend: linear regression slope over the window, normalized to mean price.
  const n = series.length;
  const meanY = series.reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - (n - 1) / 2) * (series[i] - meanY);
    den += (i - (n - 1) / 2) ** 2;
  }
  const slope = den > 0 ? num / den : 0;
  const trend = meanY > 0 ? (slope / meanY) * n : 0; // total fractional drift over window
  const last = series[series.length - 1];
  const smaShort = series.slice(-12).reduce((a, b) => a + b, 0) / Math.min(12, series.length);

  const highVol = realizedVol > 0.35;
  const crisisVol = realizedVol > 0.80;
  const bullish = trend > 0.01 && last >= smaShort && rsiVal > 50;
  const bearish = trend < -0.01 && last <= smaShort && rsiVal < 45;

  let regime = 'transition';
  if (crisisVol && bearish) regime = 'liquidity_crisis';
  else if (highVol && bearish) regime = 'high_vol_bear';
  else if (!highVol && bullish) regime = 'low_vol_bull';
  else if (Math.abs(trend) <= 0.01 && !highVol) regime = 'range_bound_choppy';

  // Confidence from signal agreement.
  let agree = 0;
  if ((trend > 0) === bullish) agree++;
  if ((last >= meanY) === bullish) agree++;
  if ((rsiVal > 50) === bullish) agree++;
  if ((rsiVal < 50) === bearish) agree++;
  const confidence = Math.min(95, Math.max(30, Math.round(40 + (agree / 4) * 50 + Math.abs(trend) * 200)));

  return {
    market_regime: regime,
    regime_confidence: confidence,
    regime_strategy: REGIME_STRATEGY[regime],
    position_multiplier: REGIME_POSITION_MULTIPLIER[regime],
    realized_vol: Math.round(realizedVol * 1000) / 1000,
    rsi: Math.round(rsiVal * 10) / 10,
    trend: Math.round(trend * 10000) / 10000,
  };
}

// classifyRegimeFromSnapshots — load the benchmark (SPY) snapshot series and classify.
export async function classifyRegimeFromSnapshots(sr, benchmark = 'SPY') {
  const snaps = await sr.entities.PriceSnapshot.list('-timestamp', 500);
  const bench = snaps.filter((s) => String(s.symbol).toUpperCase() === benchmark.toUpperCase());
  if (bench.length < 10) {
    return {
      market_regime: 'transition', regime_confidence: 30,
      regime_strategy: REGIME_STRATEGY.transition,
      position_multiplier: REGIME_POSITION_MULTIPLIER.transition,
      realized_vol: null, rsi: null, trend: null, source: 'insufficient_data',
    };
  }
  const closes = bench.map((s) => s.price).reverse(); // oldest-first
  const regime = classifyRegime(closes);
  return { ...regime, source: 'deterministic_spy_snapshots' };
}