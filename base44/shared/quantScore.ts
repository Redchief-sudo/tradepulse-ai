// Deterministic quant factor scores computed from OHLCV.
// Technical / Momentum / Risk are precise formulas (no LLM).
// Missing fundamental and sentiment inputs are neutral (50). The resulting
// composite is a weighted heuristic, not a trained machine-learning prediction.

import { rsi, macd, bollinger, sma, momentum, volatility, atr } from './indicators.ts';

export function computeRealFactors(candles) {
  if (!candles || candles.length < 30) return null;
  const closes = candles.map((c) => c.close);
  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);
  const last = closes[closes.length - 1];

  const rsiVal = rsi(closes, 14);
  const macdVal = macd(closes);
  const boll = bollinger(closes, 20, 2);
  const ma50 = sma(closes, 50);
  const ma200 = sma(closes, 200);
  const mom = momentum(closes, 14);
  const vol = volatility(closes, 20);
  const atrVal = atr(highs, lows, closes, 14);

  let technical = 50;
  if (rsiVal != null) technical += (50 - rsiVal) * 0.5;
  if (macdVal) technical += macdVal.histogram > 0 ? 10 : -10;
  if (ma50 != null && ma200 != null) technical += ma50 > ma200 ? 10 : -10;
  if (boll) {
    if (boll.percentB < 20) technical += 8;
    else if (boll.percentB > 80) technical -= 8;
  }
  technical = Math.max(0, Math.min(100, technical));

  let momentumScore = 50 + (mom || 0) * 2;
  momentumScore = Math.max(0, Math.min(100, momentumScore));

  let riskScore = 100 - (vol || 50);
  riskScore = Math.max(0, Math.min(100, riskScore));

  return {
    price: last,
    rsi: rsiVal,
    macd: macdVal,
    bollinger: boll,
    ma50,
    ma200,
    momentum: mom,
    volatility: vol,
    atr: atrVal,
    technical_score: technical,
    momentum_score: momentumScore,
    risk_score: riskScore,
  };
}

export function weightedComposite(scores, weights) {
  const w = weights || { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };
  const sum = (w.technical + w.fundamental + w.sentiment + w.momentum + w.risk) || 1;
  return (
    (scores.technical ?? 50) * w.technical / sum +
    (scores.fundamental ?? 50) * w.fundamental / sum +
    (scores.sentiment ?? 50) * w.sentiment / sum +
    (scores.momentum ?? 50) * w.momentum / sum +
    (scores.risk ?? 50) * w.risk / sum
  );
}

export function signalFromComposite(c) {
  if (c > 80) return 'STRONG_BUY';
  if (c > 65) return 'BUY';
  if (c > 45) return 'HOLD';
  if (c > 30) return 'SELL';
  return 'STRONG_SELL';
}
