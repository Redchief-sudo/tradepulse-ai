// Real technical indicator formulas — ported verbatim from src/lib/indicators.js.
// Computed from actual OHLCV price series. No LLM, no estimation.

export function sma(values, period) {
  if (!values || values.length < period) return null;
  let sum = 0;
  for (let i = values.length - period; i < values.length; i++) sum += values[i];
  return sum / period;
}

export function emaSeries(values, period) {
  if (!values || !values.length) return [];
  const k = 2 / (period + 1);
  const out = [values[0]];
  for (let i = 1; i < values.length; i++) {
    out.push(values[i] * k + out[i - 1] * (1 - k));
  }
  return out;
}

export function ema(values, period) {
  const s = emaSeries(values, period);
  return s.length ? s[s.length - 1] : null;
}

export function rsi(closes, period = 14) {
  if (!closes || closes.length < period + 1) return null;
  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
  }
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

export function macd(closes, fast = 12, slow = 26, signal = 9) {
  if (!closes || closes.length < slow + signal) return null;
  const fastEma = emaSeries(closes, fast);
  const slowEma = emaSeries(closes, slow);
  const macdLine = fastEma.map((v, i) => v - slowEma[i]);
  const signalLine = emaSeries(macdLine, signal);
  const i = macdLine.length - 1;
  return {
    macd: macdLine[i],
    signal: signalLine[i],
    histogram: macdLine[i] - signalLine[i],
  };
}

export function bollinger(closes, period = 20, mult = 2) {
  if (!closes || closes.length < period) return null;
  let sum = 0;
  for (let i = closes.length - period; i < closes.length; i++) sum += closes[i];
  const mean = sum / period;
  let variance = 0;
  for (let i = closes.length - period; i < closes.length; i++) variance += (closes[i] - mean) ** 2;
  variance /= period;
  const sd = Math.sqrt(variance);
  const last = closes[closes.length - 1];
  const width = mult * sd;
  return {
    upper: mean + width,
    middle: mean,
    lower: mean - width,
    percentB: width > 0 ? ((last - (mean - width)) / (2 * width)) * 100 : 50,
  };
}

export function atr(highs, lows, closes, period = 14) {
  if (!closes || closes.length < period + 1) return null;
  const trs = [];
  for (let i = 1; i < closes.length; i++) {
    const tr = Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i] - closes[i - 1])
    );
    trs.push(tr);
  }
  let a = 0;
  for (let i = 0; i < period; i++) a += trs[i];
  a /= period;
  for (let i = period; i < trs.length; i++) {
    a = (a * (period - 1) + trs[i]) / period;
  }
  return a;
}

export function momentum(closes, period = 14) {
  if (!closes || closes.length < period + 1) return null;
  const past = closes[closes.length - 1 - period];
  return ((closes[closes.length - 1] - past) / past) * 100;
}

export function volatility(closes, period = 20) {
  if (!closes || closes.length < period + 1) return null;
  const returns = [];
  for (let i = closes.length - period; i < closes.length; i++) {
    returns.push((closes[i] - closes[i - 1]) / closes[i - 1]);
  }
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  const variance = returns.reduce((a, b) => a + (b - mean) ** 2, 0) / returns.length;
  return Math.sqrt(variance) * Math.sqrt(365) * 100;
}