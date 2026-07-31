// Deterministic backtesting + walk-forward engine.
// Event-driven, bar-by-bar simulation over a daily OHLCV series with transaction
// costs (via costModel), deterministic indicator computation, and full performance
// metrics. No LLM estimation — every number is computed from the price series.
//
// A "strategy" is a pure function: (barIndex, series, position, equity, params) =>
// { side: 'long'|'flat', qtyFraction, target, stop } | null.
// The engine handles order execution, cost deduction, equity tracking, and metrics.

import { estimateCosts } from './costModel.ts';

function avg(arr) {
  if (!arr.length) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

function rsi(closes, period = 14) {
  if (closes.length < period + 1) return 50;
  let gains = 0, losses = 0;
  for (let i = closes.length - period; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff >= 0) gains += diff; else losses -= diff;
  }
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = (gains / period) / avgLoss;
  return 100 - 100 / (1 + rs);
}

// Built-in strategies — pure, deterministic, parameterized.
export const STRATEGIES = {
  sma_cross: {
    name: 'SMA Crossover (20/50)',
    params: { fast: 20, slow: 50 },
    signal(idx, series, pos, equity, p) {
      if (idx < p.slow) return null;
      const closes = series.slice(0, idx + 1).map((c) => c.close);
      const smaFast = avg(closes.slice(-p.fast));
      const smaSlow = avg(closes.slice(-p.slow));
      if (smaFast > smaSlow && pos.shares <= 0) return { side: 'long', qtyFraction: 1 };
      if (smaFast < smaSlow && pos.shares > 0) return { side: 'flat', qtyFraction: 0 };
      return null;
    },
  },
  rsi_reversion: {
    name: 'RSI Mean-Reversion (14, 30/70)',
    params: { period: 14, oversold: 30, overbought: 70 },
    signal(idx, series, pos, equity, p) {
      if (idx < p.period + 1) return null;
      const closes = series.slice(0, idx + 1).map((c) => c.close);
      const r = rsi(closes, p.period);
      if (r < p.oversold && pos.shares <= 0) return { side: 'long', qtyFraction: 1 };
      if (r > p.overbought && pos.shares > 0) return { side: 'flat', qtyFraction: 0 };
      return null;
    },
  },
  breakout: {
    name: 'Donchian Breakout (20)',
    params: { period: 20 },
    signal(idx, series, pos, equity, p) {
      if (idx < p.period) return null;
      const window = series.slice(idx - p.period, idx).map((c) => c.high);
      const upper = Math.max(...window);
      if (series[idx].close > upper && pos.shares <= 0) return { side: 'long', qtyFraction: 1 };
      // exit on trailing low break
      const lows = series.slice(idx - p.period, idx).map((c) => c.low);
      const lower = Math.min(...lows);
      if (series[idx].close < lower && pos.shares > 0) return { side: 'flat', qtyFraction: 0 };
      return null;
    },
  },
};

export function getStrategy(id) {
  return STRATEGIES[id] || null;
}

// runBacktest — simulate a strategy over a daily OHLCV series.
// series: [{ date, open, high, low, close, volume }] oldest-first.
// Returns { metrics, trades, equityCurve }.
export function runBacktest(series, strategy, config = {}) {
  const initialCapital = config.initialCapital || 10000;
  const assetClass = config.assetClass || 'stocks';
  const params = { ...strategy.params, ...(config.params || {}) };
  let cash = initialCapital;
  let pos = { shares: 0, avgPrice: 0 };
  const trades = [];
  const equityCurve = [];
  let peak = initialCapital;
  let maxDD = 0;
  let investedBars = 0;

  for (let i = 0; i < series.length; i++) {
    const bar = series[i];
    const equity = cash + pos.shares * bar.close;
    const sig = strategy.signal(i, series, pos, equity, params);
    if (sig) {
      if (sig.side === 'long' && pos.shares <= 0) {
        const notional = equity * (sig.qtyFraction || 1);
        const costs = estimateCosts(assetClass, notional);
        const qty = Math.floor((notional - costs.one_way_cost) / bar.close);
        if (qty > 0) {
          cash -= qty * bar.close + costs.one_way_cost;
          pos = { shares: qty, avgPrice: bar.close };
          trades.push({ date: bar.date, side: 'buy', qty, price: bar.close, cost: costs.one_way_cost });
        }
      } else if (sig.side === 'flat' && pos.shares > 0) {
        const notional = pos.shares * bar.close;
        const costs = estimateCosts(assetClass, notional);
        const pnl = (bar.close - pos.avgPrice) * pos.shares - costs.one_way_cost;
        cash += pos.shares * bar.close - costs.one_way_cost;
        trades.push({ date: bar.date, side: 'sell', qty: pos.shares, price: bar.close, cost: costs.one_way_cost, pnl });
        pos = { shares: 0, avgPrice: 0 };
      }
    }
    const eq = cash + pos.shares * bar.close;
    equityCurve.push({ date: bar.date, equity: Math.round(eq * 100) / 100 });
    if (eq > peak) peak = eq;
    const dd = peak > 0 ? (peak - eq) / peak : 0;
    if (dd > maxDD) maxDD = dd;
    if (pos.shares > 0) investedBars++;
  }

  // Mark-to-market the open position at the final bar for realized metrics.
  const finalBar = series[series.length - 1];
  let realizedTrades = trades;
  if (pos.shares > 0 && finalBar) {
    const notional = pos.shares * finalBar.close;
    const costs = estimateCosts(assetClass, notional);
    const pnl = (finalBar.close - pos.avgPrice) * pos.shares - costs.one_way_cost;
    realizedTrades = [...trades, { date: finalBar.date, side: 'sell', qty: pos.shares, price: finalBar.close, cost: costs.one_way_cost, pnl, open: true }];
  }

  const metrics = computeMetrics(equityCurve, realizedTrades, initialCapital, maxDD, series.length, investedBars);
  return { metrics, trades: realizedTrades, equityCurve };
}

function computeMetrics(equityCurve, trades, initialCapital, maxDD, totalBars, investedBars) {
  const finalEquity = equityCurve.length ? equityCurve[equityCurve.length - 1].equity : initialCapital;
  const totalReturn = (finalEquity - initialCapital) / initialCapital;
  const years = totalBars / 252;
  const cagr = years > 0 && finalEquity > 0 ? Math.pow(finalEquity / initialCapital, 1 / years) - 1 : 0;

  // Daily returns for Sharpe/Sortino.
  const rets = [];
  for (let i = 1; i < equityCurve.length; i++) {
    const prev = equityCurve[i - 1].equity;
    if (prev > 0) rets.push((equityCurve[i].equity - prev) / prev);
  }
  const mean = avg(rets);
  const sd = std(rets);
  const sharpe = sd > 0 ? (mean / sd) * Math.sqrt(252) : 0;
  const downside = rets.filter((r) => r < 0);
  const dsd = downside.length > 1 ? std(downside) : 0;
  const sortino = dsd > 0 ? (mean / dsd) * Math.sqrt(252) : 0;

  const sellTrades = trades.filter((t) => t.side === 'sell');
  const wins = sellTrades.filter((t) => (t.pnl || 0) > 0);
  const losses = sellTrades.filter((t) => (t.pnl || 0) <= 0);
  const grossProfit = wins.reduce((s, t) => s + t.pnl, 0);
  const grossLoss = Math.abs(losses.reduce((s, t) => s + t.pnl, 0));
  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? Infinity : 0);
  const winRate = sellTrades.length ? wins.length / sellTrades.length : 0;
  const avgWin = wins.length ? grossProfit / wins.length : 0;
  const avgLoss = losses.length ? grossLoss / losses.length : 0;
  const totalCosts = trades.reduce((s, t) => s + (t.cost || 0), 0);

  return {
    initial_capital: initialCapital,
    final_equity: Math.round(finalEquity * 100) / 100,
    total_return_pct: Math.round(totalReturn * 10000) / 100,
    cagr_pct: Math.round(cagr * 10000) / 100,
    sharpe: Math.round(sharpe * 100) / 100,
    sortino: Math.round(sortino * 100) / 100,
    max_drawdown_pct: Math.round(maxDD * 10000) / 100,
    num_trades: sellTrades.length,
    win_rate_pct: Math.round(winRate * 10000) / 100,
    profit_factor: Math.round(profitFactor * 100) / 100,
    avg_win: Math.round(avgWin * 100) / 100,
    avg_loss: Math.round(avgLoss * 100) / 100,
    total_costs: Math.round(totalCosts * 100) / 100,
    exposure_pct: totalBars ? Math.round((investedBars / totalBars) * 10000) / 100 : 0,
  };
}

function std(arr) {
  if (arr.length < 2) return 0;
  const mean = avg(arr);
  const variance = arr.reduce((a, b) => a + (b - mean) ** 2, 0) / (arr.length - 1);
  return Math.sqrt(variance);
}

// walkForward — split the series into rolling in-sample (train) / out-of-sample
// (test) windows. Run the strategy on each, and report IS vs OOS metrics so
// overfitting is visible (large IS/OOS degradation = curve-fit).
export function walkForward(series, strategy, config = {}) {
  const trainSize = config.trainSize || 252;   // 1 year train
  const testSize = config.testSize || 63;      // 3 months test
  const step = config.step || testSize;        // non-overlapping test windows
  const folds = [];
  for (let start = 0; start + trainSize + testSize <= series.length; start += step) {
    const train = series.slice(start, start + trainSize);
    const test = series.slice(start + trainSize, start + trainSize + testSize);
    const is = runBacktest(train, strategy, config);
    const oos = runBacktest(test, strategy, config);
    folds.push({
      train_start: train[0]?.date,
      train_end: train[train.length - 1]?.date,
      test_start: test[0]?.date,
      test_end: test[test.length - 1]?.date,
      in_sample: is.metrics,
      out_of_sample: oos.metrics,
      degradation_sharpe: is.metrics.sharpe - oos.metrics.sharpe,
    });
  }
  if (!folds.length) return { folds: [], error: 'Not enough data for walk-forward (need train+test bars).' };
  // Aggregate OOS performance across folds.
  const oosReturns = folds.map((f) => f.out_of_sample.total_return_pct);
  const avgOosReturn = avg(oosReturns);
  const avgIsReturn = avg(folds.map((f) => f.in_sample.total_return_pct));
  return {
    folds,
    summary: {
      folds_run: folds.length,
      avg_in_sample_return_pct: Math.round(avgIsReturn * 100) / 100,
      avg_out_of_sample_return_pct: Math.round(avgOosReturn * 100) / 100,
      overfitting_ratio: avgIsReturn !== 0 ? Math.round((avgOosReturn / avgIsReturn) * 100) / 100 : null,
    },
  };
}