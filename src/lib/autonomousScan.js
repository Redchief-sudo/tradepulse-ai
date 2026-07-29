import { base44 } from '@/api/base44Client';
import { computeSectorExposure } from './portfolio';

function buildPortfolioContext(holdings) {
  if (!holdings.length) return 'No current positions.';
  return holdings
    .map(
      (h) =>
        `${h.symbol} (${h.company_name || h.symbol}, ${h.shares} shares, $${h.avg_price} avg, $${h.current_price || h.avg_price} current, sector: ${h.sector || 'Other'})`
    )
    .join('\n');
}

function buildSectorContext(holdings) {
  const { sectors, total } = computeSectorExposure(holdings);
  if (!sectors.length) return 'No sector exposure yet.';
  return sectors
    .map((s) => `${s.sector}: ${s.percent.toFixed(1)}% ($${s.value.toFixed(0)})`)
    .join(', ');
}

// Pass 1 — Deep Market Scan using Gemini 3.1 Pro (top-tier with web search)
export async function runPass1(holdings) {
  const portfolioContext = buildPortfolioContext(holdings);
  const sectorContext = buildSectorContext(holdings);

  return await base44.integrations.Core.InvokeLLM({
    prompt: `You are AlphaTrade AI. PASS 1 — Deep Market Scan using real-time data.

Current portfolio:
${portfolioContext}

Current sector exposure:
${sectorContext}

Scan today's real-time market and identify 5-7 high-potential candidate stocks. For each, provide a comprehensive analysis with FULL TECHNICAL INDICATORS:
- Current price and recent performance (1-day, 1-week, 1-month returns)
- Key fundamentals: P/E ratio, revenue growth %, profit margins, debt-to-equity, ROE
- Technical indicators: RSI (0-100), MACD signal (bullish/bearish), 50-day MA, 200-day MA, Bollinger Band position, volume trend, support and resistance levels
- Recent news, earnings, analyst ratings, and catalysts
- Sector classification
- Correlation vs existing portfolio holdings

Prioritize stocks with strong setups that would IMPROVE diversification. Also flag weak current holdings as potential sells.`,
    add_context_from_internet: true,
    model: 'gemini_3_1_pro',
    response_json_schema: {
      type: 'object',
      properties: {
        market_summary: { type: 'string' },
        candidates: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              company_name: { type: 'string' },
              sector: { type: 'string' },
              current_price: { type: 'number' },
              fundamentals: { type: 'string' },
              technicals: { type: 'string' },
              rsi: { type: 'number' },
              macd_signal: { type: 'string', enum: ['bullish', 'bearish', 'neutral'] },
              ma50: { type: 'number' },
              ma200: { type: 'number' },
              bollinger_position: { type: 'string', enum: ['upper', 'middle', 'lower'] },
              volume_trend: { type: 'string', enum: ['increasing', 'stable', 'decreasing'] },
              support_level: { type: 'number' },
              resistance_level: { type: 'number' },
              news_catalysts: { type: 'string' },
              recommendation: {
                type: 'string',
                enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
              },
              confidence: { type: 'number' },
              target_price: { type: 'number' },
              stop_loss: { type: 'number' },
              summary: { type: 'string' },
            },
          },
        },
      },
      required: ['market_summary', 'candidates'],
    },
  });
}

// Pass 2 — Portfolio Fit & Risk-Aware Selection using Claude Sonnet 5 (top-tier reasoning)
export async function runPass2(holdings, candidates, profile) {
  const pp = profile || { max_position_pct: 10, max_sector_pct: 25, min_confidence: 80, max_daily_trades: 5, stop_loss_pct: 8 };
  const portfolioContext = buildPortfolioContext(holdings);
  const sectorContext = buildSectorContext(holdings);
  const { total } = computeSectorExposure(holdings);

  const candidatesCompact = (candidates || []).map((c) => ({
    symbol: c.symbol,
    company_name: c.company_name,
    sector: c.sector,
    current_price: c.current_price,
    recommendation: c.recommendation,
    confidence: c.confidence,
    target_price: c.target_price,
    stop_loss: c.stop_loss,
    rsi: c.rsi,
    macd_signal: c.macd_signal,
    ma50: c.ma50,
    ma200: c.ma200,
    summary: c.summary,
  }));

  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are AlphaTrade AI. PASS 2 — Portfolio Fit & Risk-Aware Selection (Claude Sonnet 5 reasoning engine).

Current portfolio:
${portfolioContext}

Current sector exposure (CRITICAL for diversification):
${sectorContext}
Total portfolio value: $${total.toFixed(0)}

Candidate analyses from Pass 1:
${JSON.stringify(candidatesCompact, null, 2)}

Select the best trades to execute NOW. Apply these INSTITUTIONAL-GRADE risk parameters STRICTLY:
- MAX POSITION SIZE: ${pp.max_position_pct}% of portfolio per single trade (HARD CAP)
- MAX SECTOR EXPOSURE: ${pp.max_sector_pct}% per sector including the new position (HARD CAP)
- MIN CONFIDENCE: ${pp.min_confidence}% — do NOT propose any trade with confidence below this
- MAX TRADES PER SCAN: ${pp.max_daily_trades}
- STOP-LOSS: set ${pp.stop_loss_pct}% below entry price for every buy

Rules:
1. RISK-AWARE REBALANCING: Prioritize buys in UNDERWEIGHT sectors. For sectors already above ${pp.max_sector_pct}%, prefer sells or trim.
2. CORRELATION-AWARE: Avoid adding multiple highly-correlated stocks. Don't pile into the same sector.
3. CONFIDENCE-WEIGHTED SIZING: For each buy, suggest suggested_position_pct (0-${pp.max_position_pct}) = % of total portfolio value. NEVER exceed ${pp.max_position_pct}% per position or ${pp.max_sector_pct}% per sector after the trade.
4. EXIT MANAGEMENT: Every buy must have a stop_loss at ${pp.stop_loss_pct}% below entry and a realistic target_price.
5. For SELL actions, only sell stocks currently in the portfolio.
6. Do not propose more than ${pp.max_daily_trades} trades.

Return final trade proposals with full reasoning, technicals, and catalysts.`,
    response_json_schema: {
      type: 'object',
      properties: {
        risk_assessment: { type: 'string' },
        proposals: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              company_name: { type: 'string' },
              sector: { type: 'string' },
              action: { type: 'string', enum: ['buy', 'sell'] },
              current_price: { type: 'number' },
              confidence: { type: 'number' },
              target_price: { type: 'number' },
              stop_loss: { type: 'number' },
              suggested_position_pct: { type: 'number' },
              recommendation: {
                type: 'string',
                enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
              },
              reasoning: { type: 'string' },
              technicals: { type: 'string' },
              news_catalysts: { type: 'string' },
            },
          },
        },
      },
      required: ['risk_assessment', 'proposals'],
    },
  });
}

// Pass 3 — ML Multi-Factor Scoring Engine using Claude Sonnet 5
export async function runPass3(proposals, candidates) {
  if (!proposals.length) return { scores: [] };

  const analysisData = (candidates || []).map((c) => ({
    symbol: c.symbol,
    sector: c.sector,
    current_price: c.current_price,
    fundamentals: c.fundamentals,
    technicals: c.technicals,
    rsi: c.rsi,
    macd_signal: c.macd_signal,
    ma50: c.ma50,
    ma200: c.ma200,
    bollinger_position: c.bollinger_position,
    volume_trend: c.volume_trend,
    support_level: c.support_level,
    resistance_level: c.resistance_level,
    news_catalysts: c.news_catalysts,
    recommendation: c.recommendation,
    confidence: c.confidence,
  }));

  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are AlphaTrade AI's Machine Learning scoring engine — a multi-factor quantitative model (like those used in hedge funds).

For each proposed trade, compute scores (0-100) across 5 factors using the analysis data below:

FACTOR DEFINITIONS:
1. TECHNICAL SCORE (0-100):
   - RSI: 30-50 = bullish/oversold, 50-70 = neutral, >70 = overbought/bearish
   - 50/200 MA alignment: golden cross (MA50 > MA200) = bullish
   - MACD signal: bullish = +, bearish = -, neutral = 0
   - Bollinger position: lower band = bounce potential, upper = reversal risk
   - Support/resistance proximity

2. FUNDAMENTAL SCORE (0-100):
   - P/E ratio: <15 = undervalued, 15-25 = fair, >35 = overvalued
   - Revenue growth: >20% = strong, 10-20% = moderate, <5% = weak
   - Profit margins and ROE
   - Debt levels

3. SENTIMENT SCORE (0-100):
   - News sentiment (positive/negative/neutral)
   - Analyst ratings and upgrades/downgrades
   - Earnings surprises
   - Social buzz and catalyst strength

4. MOMENTUM SCORE (0-100):
   - Short-term price momentum
   - Relative strength vs sector and market
   - Volume trend (increasing = confirming move)

5. RISK SCORE (0-100, INVERTED — lower risk = higher score):
   - Volatility/beta
   - Drawdown potential
   - Sector concentration risk
   - Correlation with existing portfolio

COMPOSITE ML SCORE = weighted average:
- Technical: 25%
- Fundamental: 25%
- Sentiment: 20%
- Momentum: 15%
- Risk: 15%

ML SIGNAL:
- >80: STRONG_BUY
- 65-80: BUY
- 45-65: HOLD
- 30-45: SELL
- <30: STRONG_SELL

Proposed trades to score:
${JSON.stringify(proposals.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector, current_price: p.current_price, confidence: p.confidence })), null, 2)}

Candidate analysis data:
${JSON.stringify(analysisData, null, 2)}

Score each proposal. Provide a brief score_reasoning explaining the key drivers of the composite score.`,
    response_json_schema: {
      type: 'object',
      properties: {
        scores: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              technical_score: { type: 'number' },
              fundamental_score: { type: 'number' },
              sentiment_score: { type: 'number' },
              momentum_score: { type: 'number' },
              risk_score: { type: 'number' },
              ml_score: { type: 'number' },
              ml_signal: {
                type: 'string',
                enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
              },
              score_reasoning: { type: 'string' },
            },
          },
        },
      },
      required: ['scores'],
    },
  });
}