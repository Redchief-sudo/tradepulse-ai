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

export async function runAutonomousScan(holdings) {
  const portfolioContext = buildPortfolioContext(holdings);
  const sectorContext = buildSectorContext(holdings);
  const { total } = computeSectorExposure(holdings);

  // Pass 1 — Market Deep Scan: identify candidates and analyze each deeply
  const pass1 = await base44.integrations.Core.InvokeLLM({
    prompt: `You are AlphaTrade AI. PASS 1 — Market Deep Scan.

Current portfolio:
${portfolioContext}

Current sector exposure (use for diversification):
${sectorContext}

Scan today's real-time market and identify 5-7 high-potential candidate stocks. For each, provide a DEEP analysis:
- Current price and recent performance
- Key fundamentals (P/E ratio, revenue growth, profit margins, debt)
- Technical analysis: RSI (0-100), 50-day MA, 200-day MA, key support/resistance levels
- Recent news, earnings, and catalysts that could move the stock
- Sector classification
- Correlation considerations vs the existing portfolio

Prioritize stocks that would IMPROVE portfolio diversification (underweight sectors) and have strong technical+fundamental setups. Also flag any current holdings that look weak (potential sells).`,
    add_context_from_internet: true,
    model: 'gemini_3_flash',
    response_json_schema: {
      type: 'object',
      properties: {
        market_summary: {
          type: 'string',
          description: "Overview of today's market conditions, trends, and sentiment",
        },
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

  // Pass 2 — Portfolio Fit & Risk-Aware Selection with position sizing
  const candidatesCompact = (pass1.candidates || []).map((c) => ({
    symbol: c.symbol,
    company_name: c.company_name,
    sector: c.sector,
    current_price: c.current_price,
    recommendation: c.recommendation,
    confidence: c.confidence,
    target_price: c.target_price,
    stop_loss: c.stop_loss,
    rsi: c.rsi,
    summary: c.summary,
  }));

  const pass2 = await base44.integrations.Core.InvokeLLM({
    prompt: `You are AlphaTrade AI. PASS 2 — Portfolio Fit & Risk-Aware Selection.

Current portfolio:
${portfolioContext}

Current sector exposure (CRITICAL for diversification):
${sectorContext}
Total portfolio value: $${total.toFixed(0)}

Candidate analyses from Pass 1:
${JSON.stringify(candidatesCompact, null, 2)}

Select the best 3-5 trades to execute NOW. Apply these rules strictly:
1. RISK-AWARE REBALANCING: Prioritize buys in UNDERWEIGHT sectors (< 20% exposure). For OVERWEIGHT sectors (> 40%), prefer sells or trim positions.
2. CORRELATION-AWARE: Avoid adding multiple highly-correlated stocks. Don't pile into the same sector.
3. CONFIDENCE-WEIGHTED SIZING: For each buy, suggest suggested_position_pct (0-25) = what % of total portfolio value to allocate. Higher confidence = larger position. Respect 25% max per position and 40% max per sector AFTER the trade.
4. EXIT MANAGEMENT: Every buy must have a realistic stop_loss (typically 8-15% below entry) and target_price (realistic upside).
5. For SELL actions, only sell stocks currently in the portfolio.

Return final trade proposals with full reasoning, technicals, and catalysts.`,
    response_json_schema: {
      type: 'object',
      properties: {
        risk_assessment: {
          type: 'string',
          description: 'Assessment of portfolio risk and how these trades improve it',
        },
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
              suggested_position_pct: {
                type: 'number',
                description: '0-25, percentage of total portfolio value to allocate',
              },
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

  return {
    marketSummary: pass1.market_summary || '',
    candidates: pass1.candidates || [],
    riskAssessment: pass2.risk_assessment || '',
    proposals: pass2.proposals || [],
  };
}