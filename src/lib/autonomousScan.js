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
export async function runPass1(holdings, assetClasses) {
  const portfolioContext = buildPortfolioContext(holdings);
  const sectorContext = buildSectorContext(holdings);
  const classes = assetClasses && assetClasses.length ? assetClasses : ['stocks', 'crypto', 'forex', 'commodities', 'fixed_income'];
  const classMap = {
    stocks: 'US Equities (large-cap, mid-cap growth)',
    crypto: 'Cryptocurrency (BTC, ETH, SOL, Layer-1s, DeFi majors)',
    forex: 'Forex pairs (EUR/USD, USD/JPY, GBP/USD, AUD/USD)',
    commodities: 'Commodities (Gold, Silver, Crude Oil, Natural Gas, Wheat)',
    fixed_income: 'Fixed Income & Yields (US 10-Yr Treasury Yield, Corporate Bond ETFs)',
  };
  const assetScope = classes.map((c) => classMap[c] || c).join('; ');

  return await base44.integrations.Core.InvokeLLM({
    prompt: `You are AlphaTrade AI. PASS 1 — Multi-Modal Deep Market Scan across MULTIPLE ASSET CLASSES (Transformer architecture for time-series + financial NLP sentiment).

Ingest data across ALL modalities:
- Structured market data: price, volume, order book flow, options activity, tick-by-tick trends
- Alternative datasets: supply chain logistics, consumer spending trends, foot-traffic signals where available
- Unstructured text: real-time SEC filings, earnings call transcripts, analyst notes, global breaking news — extract semantic sentiment

Current portfolio:
${portfolioContext}

Current sector exposure:
${sectorContext}

ASSET CLASSES IN SCOPE: ${assetScope}

Scan today's real-time market across these asset classes and identify 5-7 high-potential candidates. For each, provide a comprehensive analysis with FULL TECHNICAL INDICATORS:
- Asset class (stocks, crypto, forex, commodities, fixed_income)
- Current price and recent performance (1-day, 1-week, 1-month returns)
- Key fundamentals: P/E, revenue growth, margins, debt-to-equity, ROE (stocks); tokenomics, on-chain metrics, real yield (crypto); yield differentials, rate expectations (forex/bonds); supply-demand, inventory, OPEC/production (commodities)
- Technical indicators: RSI (0-100), MACD signal (bullish/bearish), 50-day MA, 200-day MA, Bollinger Band position, volume trend, support and resistance levels
- Recent news, catalysts, and macro drivers
- Sector / asset classification
- Correlation vs existing portfolio holdings
- Multi-modal signals: order book / options flow bias, alt-data momentum, and news/filing sentiment (bullish/bearish/neutral)
- MICROSTRUCTURE SIGNAL: identify the dominant microstructure/liquidity signal — e.g. "Dark Pool Block Print" (equities institutional accumulation), "Whale On-Chain Accumulation" (crypto large wallet inflows), "FX Order Imbalance" (forex directional flow), "Level 3 Order Book Pressure" (deep bid/ask imbalance), "ETF Flow Divergence" (fixed income). One-line detail.
- CROSS-ASSET CORRELATION: note any cross-asset macro link (e.g. USD strength vs Gold/Crypto, Treasury yield spike vs Growth Tech, Crude Oil spike vs Aviation/Logistics)

FIRST, detect the current market regime using HMM-style analysis (low-volatility bull, high-volatility bear, range-bound choppy, liquidity crisis, or transition). State the regime, your confidence, and which sub-strategy is optimal for it (e.g., trend-following in low_vol_bull, mean-reversion in range_bound_choppy, defensive/capital-preservation in liquidity_crisis).

Prioritize stocks with strong setups that would IMPROVE diversification AND match the current regime's optimal strategy. Also flag weak current holdings as potential sells.`,
    add_context_from_internet: true,
    model: 'gemini_3_1_pro',
    response_json_schema: {
      type: 'object',
      properties: {
        market_summary: { type: 'string' },
        market_regime: {
          type: 'string',
          enum: ['low_vol_bull', 'high_vol_bear', 'range_bound_choppy', 'liquidity_crisis', 'transition'],
        },
        regime_confidence: { type: 'number' },
        regime_strategy: { type: 'string' },
        candidates: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              company_name: { type: 'string' },
              asset_class: { type: 'string', enum: ['stocks', 'crypto', 'forex', 'commodities', 'fixed_income'] },
              sector: { type: 'string' },
              current_price: { type: 'number' },
              microstructure_signal: { type: 'string' },
              cross_asset_correlation: { type: 'string' },
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

// Pass 2 — Investment Committee Debate (4-archetype consensus)
export async function runCommitteeDebate(candidates) {
  if (!candidates.length) return { debates: [] };

  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are AlphaTrade AI's INVESTMENT COMMITTEE — a simulated multi-archetype debate panel. Each candidate asset is debated by 4 distinct investment philosophies before it can advance to execution.

THE 4 ARCHETYPES:
1. The Value/Utility Specialist (Buffett / Fundamental Analyst): Focuses on FCF, valuation, tokenomics, or real yield. Looks for margin of safety and intrinsic value.
2. The Macro Contrarian (Burry): Searches for liquidity traps, unsustainable leverage, credit/de-pegging risks, and crowded trades. Contrarian by nature.
3. The Quant Statistician (Simmons): Evaluates order-flow momentum, statistical arbitrage, mean-reversion, and quantitative edge. Data-driven, pattern-based.
4. The Tail-Risk Hedger (Taleb): Tests for Black Swan vulnerability, volatility spikes, convex payoffs, and fragility. Asymmetric-risk focused.

Candidates to debate:
${JSON.stringify(candidates.map((c) => ({ symbol: c.symbol, asset_class: c.asset_class, sector: c.sector, current_price: c.current_price, recommendation: c.recommendation, confidence: c.confidence, microstructure_signal: c.microstructure_signal, summary: c.summary })), null, 2)}

For EACH candidate, run the structured debate. Each of the 4 archetypes gives:
- vote: "bullish", "bearish", or "neutral"
- argument: one-sentence key argument
- conviction: 0-100

Then compute consensus_votes = count of bullish votes (out of 4). A candidate needs consensus_votes >= 3 (3/4 majority) to advance. Return consensus: true/false.`,
    response_json_schema: {
      type: 'object',
      properties: {
        debates: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              personas: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    name: { type: 'string', enum: ['Value/Utility Specialist', 'Macro Contrarian', 'Quant Statistician', 'Tail-Risk Hedger'] },
                    vote: { type: 'string', enum: ['bullish', 'bearish', 'neutral'] },
                    argument: { type: 'string' },
                    conviction: { type: 'number' },
                  },
                },
              },
              consensus_votes: { type: 'number' },
              consensus: { type: 'boolean' },
            },
          },
        },
      },
      required: ['debates'],
    },
  });
}

// Pass 3 — Portfolio Fit & Risk-Aware Selection using Claude Sonnet 5 (top-tier reasoning)
export async function runPass2(holdings, candidates, profile, regime) {
  const pp = profile || { max_position_pct: 10, max_sector_pct: 25, min_confidence: 80, max_daily_trades: 5, stop_loss_pct: 8 };
  const regimeContext = regime && regime.market_regime
    ? `Detected market regime: ${regime.market_regime} (${(regime.regime_confidence || 0)}% confidence). Optimal strategy: ${regime.regime_strategy || 'adaptive'}. Apply this strategy.`
    : 'Market regime: not detected — use adaptive strategy.';
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
    prompt: `You are AlphaTrade AI. PASS 2 — Portfolio Fit & Risk-Aware Selection (Reinforcement Learning / Deep Q-Network for execution & position-sizing optimization · Claude Sonnet 5 reasoning).

Current portfolio:
${portfolioContext}

Current sector exposure (CRITICAL for diversification):
${sectorContext}
Total portfolio value: $${total.toFixed(0)}

Candidate analyses from Pass 1:
${JSON.stringify(candidatesCompact, null, 2)}

${regimeContext}

Select the best trades to execute NOW. Apply these INSTITUTIONAL-GRADE risk parameters STRICTLY:
- MAX POSITION SIZE: ${pp.max_position_pct}% of portfolio per single trade (HARD CAP)
- MAX SECTOR EXPOSURE: ${pp.max_sector_pct}% per sector including the new position (HARD CAP)
- MIN CONFIDENCE: ${pp.min_confidence}% — do NOT propose any trade with confidence below this
- MAX TRADES PER SCAN: ${pp.max_daily_trades}
- STOP-LOSS: set ${pp.stop_loss_pct}% below entry price for every buy

Rules:
1. RISK-AWARE REBALANCING: Prioritize buys in UNDERWEIGHT sectors. For sectors already above ${pp.max_sector_pct}%, prefer sells or trim.
2. CORRELATION-AWARE: Avoid adding multiple highly-correlated assets. Don't pile into the same sector or asset class.
2b. CROSS-ASSET MACRO CORRELATION: Check cross-asset links before proposing — e.g. USD strength pressures Gold/Crypto, Treasury yield spikes hit Growth Tech, Crude Oil spikes hit Aviation/Logistics. Avoid trades whose cross-asset macro correlation would concentrate risk.
3. CONFIDENCE-WEIGHTED SIZING: For each buy, suggest suggested_position_pct (0-${pp.max_position_pct}) = % of total portfolio value. NEVER exceed ${pp.max_position_pct}% per position or ${pp.max_sector_pct}% per sector after the trade.
4. EXIT MANAGEMENT: Every buy must have a stop_loss at ${pp.stop_loss_pct}% below entry and a realistic target_price.
5. For SELL actions, only sell stocks currently in the portfolio.
6. DYNAMIC POSITION SIZING: Assess the current market volatility regime (high macro uncertainty / elevated VIX = high regime). In a HIGH-volatility regime, scale suggested_position_pct DOWN by up to 50% from the max. In a LOW-volatility regime, sizing may approach the max ${pp.max_position_pct}%.
7. DRAWDOWN GUARDRAIL: Ensure the portfolio's estimated max drawdown risk after all proposed trades stays within ${pp.max_drawdown_pct}%. Prefer lower-correlation trades when portfolio risk is elevated.
8. Do not propose more than ${pp.max_daily_trades} trades.

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
export async function runPass3(proposals, candidates, weights) {
  if (!proposals.length) return { scores: [] };
  const w = weights || { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };

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
    prompt: `You are AlphaTrade AI's Machine Learning scoring engine — a hybrid ensemble: multi-factor quantitative model + Graph Neural Network (GNN) for sector contagion mapping (like those used in hedge funds).

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
    - SECTOR CONTAGION (GNN): how a downturn in this stock's sector would propagate to existing holdings — high contagion = lower score

COMPOSITE ML SCORE = weighted average (SELF-LEARNING ADJUSTED WEIGHTS — reflect recent trade performance):
- Technical: ${w.technical}%
- Fundamental: ${w.fundamental}%
- Sentiment: ${w.sentiment}%
- Momentum: ${w.momentum}%
- Risk: ${w.risk}%

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

// Pass 4 — Adversarial Risk Officer (multi-agent veto layer)
export async function runAdversarialReview(proposals, holdings, regime) {
  if (!proposals.length) return { reviews: [] };
  const portfolioContext = buildPortfolioContext(holdings);
  const sectorContext = buildSectorContext(holdings);
  const regimeLabel = regime && regime.market_regime ? regime.market_regime : 'unknown';

  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are the ADVERSARIAL RISK OFFICER — a ruthlessly conservative, independent AI agent in AlphaTrade AI's multi-agent system. Your sole job is to find flaws in the Alpha Generator's trade proposals and VETO dangerous ones.

You are adversarial by design. You actively try to block trades by predicting hidden correlation risks, liquidity traps, regime mismatches, and concentration dangers.

Current portfolio:
${portfolioContext}

Current sector exposure:
${sectorContext}

Current market regime: ${regimeLabel}

Trade proposals under review:
${JSON.stringify(proposals.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector, confidence: p.confidence, suggested_position_pct: p.suggested_position_pct, ml_score: p.ml_score })), null, 2)}

For EACH proposal, return a verdict:
- "approved": the trade is sound — no hidden risk found
- "flagged": notable risk exists but trade may proceed with caution (explain the risk)
- "vetoed": the trade is dangerous and must NOT execute (hidden correlation, liquidity trap, regime mismatch, or concentration breach)

Be skeptical. When in doubt, flag. Only veto when the risk is clear and material.`,
    response_json_schema: {
      type: 'object',
      properties: {
        reviews: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              verdict: { type: 'string', enum: ['approved', 'flagged', 'vetoed'] },
              note: { type: 'string' },
            },
          },
        },
      },
      required: ['reviews'],
    },
  });
}

// Self-Learning Engine — post-mortem feedback loop on past decisions
export async function runSelfLearning(decisions) {
  if (!decisions || !decisions.length) return { weights: null, post_mortems: [], accuracy: null };
  const trades = decisions.slice(0, 30).map((d) => ({
    symbol: d.symbol,
    action: d.action,
    sector: d.sector,
    asset_class: d.asset_class,
    confidence: d.confidence,
    ml_score: d.ml_score,
    technical_score: d.technical_score,
    fundamental_score: d.fundamental_score,
    sentiment_score: d.sentiment_score,
    momentum_score: d.momentum_score,
    risk_score: d.risk_score,
    status: d.status,
    created_date: d.created_date,
  }));

  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are AlphaTrade AI's SELF-LEARNING ENGINE — a post-mortem feedback loop that diagnoses past trade performance and dynamically adjusts the 5 quant factor weights.

Past AI trade decisions (most recent 30):
${JSON.stringify(trades, null, 2)}

For trades that underperformed (hit stop-loss or lost >3%), diagnose whether the loss was caused by:
- bad technical timing
- flawed sentiment analysis
- a macro event
- poor fundamental assessment
- momentum misread

Analyze which factors have been OVER-weighted (predicting losers) vs UNDER-weighted (missing winners). Then produce ADJUSTED factor weights (must sum to 100) that improve future accuracy.

Current default weights: Technical 25, Fundamental 25, Sentiment 20, Momentum 15, Risk 15.
Adjust based on which factors actually predicted winning vs losing trades. Keep each weight between 5 and 40.

Also produce 2-4 short post-mortem diagnoses of the worst trades, and an accuracy estimate (% of past decisions that were winners).`,
    response_json_schema: {
      type: 'object',
      properties: {
        weights: {
          type: 'object',
          properties: {
            technical: { type: 'number' },
            fundamental: { type: 'number' },
            sentiment: { type: 'number' },
            momentum: { type: 'number' },
            risk: { type: 'number' },
          },
        },
        post_mortems: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              symbol: { type: 'string' },
              cause: { type: 'string' },
              lesson: { type: 'string' },
            },
          },
        },
        accuracy: { type: 'number' },
        summary: { type: 'string' },
      },
    },
  });
}

// Causal Supply Chain & Cross-Asset Contagion Engine (DAG)
export async function runCausalContagion(proposal, holdings) {
  return await base44.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are AlphaTrade AI's CAUSAL CONTAGION ENGINE. Compute a Directed Acyclic Graph (DAG) of causal contagion paths for the proposed trade — root cause drivers versus simple surface price correlation.

Proposed trade: ${proposal.symbol} (${proposal.asset_class || 'stocks'}, ${proposal.action}, sector: ${proposal.sector || 'n/a'})
Current portfolio: ${holdings.map((h) => h.symbol).join(', ') || 'empty'}

Ingest corporate relationship maps, commodity supply chains, and cross-asset liquidity dependencies. Build a causal chain of 3-5 nodes from a root macro driver to the target asset. Example: Crude Oil spike -> Aviation/Logistics stocks down -> USD strengthening -> Crypto short-term liquidity dip.

Return the contagion path as an ordered array of nodes (root cause first, target last), each with a label and a short causal mechanism to the next node. Also return a one-line root_cause summary and a contagion_risk score (0-100, higher = more systemic risk).`,
    response_json_schema: {
      type: 'object',
      properties: {
        root_cause: { type: 'string' },
        contagion_risk: { type: 'number' },
        nodes: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              label: { type: 'string' },
              mechanism: { type: 'string' },
            },
          },
        },
      },
    },
  });
}

// Predictive Algorithmic Execution — asset-specific order slicing (pure compute, no LLM)
export function computeExecutionParams(proposal) {
  const assetClass = proposal.asset_class || 'stocks';
  const positionValue = proposal.position_value || (proposal.suggested_position_pct || 5) * 1000;
  const base = {
    stocks: { algorithm: 'VWAP', venue: 'Smart-router (NYSE/NASDAQ dark)', slices: Math.max(4, Math.round(positionValue / 25000)), latency: 'Medium', slippage: 0.08 },
    crypto: { algorithm: 'TWAP + DEX routing', venue: 'CEX + on-chain DEX pools', slices: Math.max(6, Math.round(positionValue / 15000)), latency: 'Low', slippage: 0.15 },
    forex: { algorithm: 'TWAP', venue: 'ECN (Hotspot/FXAll)', slices: Math.max(5, Math.round(positionValue / 50000)), latency: 'Low', slippage: 0.02 },
    commodities: { algorithm: 'VWAP', venue: 'Futures + ETF smart-router', slices: Math.max(4, Math.round(positionValue / 30000)), latency: 'Medium', slippage: 0.10 },
    fixed_income: { algorithm: 'TWAP', venue: 'Bond ETF + dealer RFQ', slices: Math.max(3, Math.round(positionValue / 50000)), latency: 'High', slippage: 0.05 },
  };
  const cfg = base[assetClass] || base.stocks;
  return {
    algorithm: cfg.algorithm,
    venue: cfg.venue,
    slice_count: cfg.slices,
    latency_sensitivity: cfg.latency,
    expected_slippage_pct: cfg.slippage,
  };
}