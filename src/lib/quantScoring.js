// Real deterministic quant factor scores computed from actual OHLCV.
// No LLM, no estimation. Technical / Momentum / Risk are precise formulas.
// Fundamental & Sentiment are qualitative and supplied by the LLM where appropriate — never fabricated here.

import { rsi, macd, bollinger, sma, momentum, volatility, atr } from './indicators';

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

  // Technical score (0-100): RSI position + MACD histogram + MA alignment + Bollinger %B
  let technical = 50;
  if (rsiVal != null) technical += (50 - rsiVal) * 0.5; // RSI 30 → +10 (oversold bullish), 70 → -10
  if (macdVal) technical += macdVal.histogram > 0 ? 10 : -10;
  if (ma50 != null && ma200 != null) technical += ma50 > ma200 ? 10 : -10;
  if (boll) {
    if (boll.percentB < 20) technical += 8;
    else if (boll.percentB > 80) technical -= 8;
  }
  technical = Math.max(0, Math.min(100, technical));

  // Momentum score (0-100): 14-day return % scaled
  let momentumScore = 50 + (mom || 0) * 2;
  momentumScore = Math.max(0, Math.min(100, momentumScore));

  // Risk score (0-100, higher = lower risk): inverse of annualized volatility
  let riskScore = 100 - (vol || 50);
  riskScore = Math.max(0, Math.min(100, riskScore));

  // Composite from the three REAL factors (equal weight — adjustable via self-learning)
  const composite = (technical + momentumScore + riskScore) / 3;

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
    composite,
  };
}