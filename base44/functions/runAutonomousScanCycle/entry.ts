import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { getAlpacaAccount } from '../../shared/alpaca.ts';
import { settleTrade } from '../../shared/execution.ts';
import { computeRealFactors, weightedComposite, signalFromComposite } from '../../shared/quantScore.ts';

const PROFILES = {
  aggressive: { max_position_pct: 15, max_sector_pct: 40, min_confidence: 70, max_daily_trades: 8, stop_loss_pct: 12 },
  balanced: { max_position_pct: 10, max_sector_pct: 25, min_confidence: 80, max_daily_trades: 5, stop_loss_pct: 8 },
  conservative: { max_position_pct: 5, max_sector_pct: 15, min_confidence: 88, max_daily_trades: 3, stop_loss_pct: 5 },
};
function profileParams(id) { return PROFILES[id] || PROFILES.balanced; }

function sectorExposure(holdings) {
  const sectors = {};
  let total = 0;
  holdings.forEach((h) => {
    const value = h.shares * (h.current_price || h.avg_price);
    const s = h.sector || 'Other';
    sectors[s] = (sectors[s] || 0) + value;
    total += value;
  });
  return {
    sectors: Object.entries(sectors).map(([sector, value]) => ({ sector, value, percent: total > 0 ? (value / total) * 100 : 0 })),
    total,
  };
}
function portfolioContext(holdings) {
  if (!holdings.length) return 'No current positions.';
  return holdings.map((h) => `${h.symbol} (${h.company_name || h.symbol}, ${h.shares} shares, $${h.avg_price} avg, $${h.current_price || h.avg_price} current, sector: ${h.sector || 'Other'})`).join('\n');
}

const FINNHUB_CANDLE = 'https://finnhub.io/api/v1/stock/candle';
async function fetchCandles(symbol, key) {
  try {
    const to = Math.floor(Date.now() / 1000);
    const from = to - 220 * 86400;
    const res = await fetch(`${FINNHUB_CANDLE}?symbol=${encodeURIComponent(symbol)}&resolution=D&from=${from}&to=${to}&token=${key}`);
    const d = await res.json();
    if (!d || d.s !== 'ok' || !d.c || d.c.length < 30) return null;
    return d.t.map((t, i) => ({ t, open: d.o[i], high: d.h[i], low: d.l[i], close: d.c[i], volume: d.v[i] }));
  } catch (e) {
    return null;
  }
}

// Full 5-pass autonomous AI trading cycle with REAL deterministic scoring.
// Pass 1: Gemini scan -> Pass 2: Claude portfolio fit -> Committee debate ->
// Deterministic ML scoring (real indicators) -> Adversarial veto -> Causal contagion ->
// Execute -> Self-learning weight update -> Email alert.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const sr = base44.asServiceRole;
    const key = secrets.get('FINNHUB_API_KEY');
    const holdings = await sr.entities.Holding.list();
    const pp = profileParams(user.trade_profile || 'balanced');
    const weights = user.ml_weights || { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };
    const pCtx = portfolioContext(holdings);
    const sec = sectorExposure(holdings);
    const secCtx = sec.sectors.length ? sec.sectors.map((s) => `${s.sector}: ${s.percent.toFixed(1)}% ($${s.value.toFixed(0)})`).join(', ') : 'No sector exposure yet.';

    // PASS 1 — Multi-asset deep market scan (Gemini 3.1 Pro, web search)
    const p1 = await sr.integrations.Core.InvokeLLM({
      prompt: `You are AlphaTrade AI. PASS 1 — Multi-asset deep market scan.\nCurrent portfolio:\n${pCtx}\n\nSector exposure:\n${secCtx}\n\nScan today's real-time US equity market and identify 5-7 high-potential candidates. For each: symbol, company_name, sector, current_price, recommendation (STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL), confidence (0-100), target_price, stop_loss, fundamentals (P/E, revenue growth, margins), news_catalysts, and a one-line summary. Flag weak current holdings as sells. Prioritize setups that improve diversification.`,
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
                symbol: { type: 'string' }, company_name: { type: 'string' }, sector: { type: 'string' },
                current_price: { type: 'number' }, recommendation: { type: 'string', enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'] },
                confidence: { type: 'number' }, target_price: { type: 'number' }, stop_loss: { type: 'number' },
                fundamentals: { type: 'string' }, news_catalysts: { type: 'string' }, summary: { type: 'string' },
              },
            },
          },
        },
        required: ['market_summary', 'candidates'],
      },
    });
    const candidates = p1.candidates || [];
    if (!candidates.length) return Response.json({ ok: true, message: 'No candidates', market_summary: p1.market_summary });

    // Enrich each candidate with REAL indicators from live daily candles
    const enriched = [];
    for (const c of candidates) {
      const candles = key ? await fetchCandles(c.symbol, key) : null;
      const factors = candles ? computeRealFactors(candles) : null;
      enriched.push({ ...c, realFactors: factors, realPrice: factors?.price || c.current_price });
    }

    // PASS 2 — Portfolio fit & risk-aware sizing (Claude Sonnet 5)
    const p2 = await sr.integrations.Core.InvokeLLM({
      model: 'claude-sonnet-5',
      prompt: `You are AlphaTrade AI. PASS 2 — Portfolio fit & risk-aware selection.\nCurrent portfolio:\n${pCtx}\n\nSector exposure:\n${secCtx}\nTotal value: $${sec.total.toFixed(0)}\n\nCandidates (with REAL computed indicators where available):\n${JSON.stringify(enriched.map((e) => ({ symbol: e.symbol, sector: e.sector, current_price: e.realPrice, recommendation: e.recommendation, confidence: e.confidence, target_price: e.target_price, stop_loss: e.stop_loss, rsi: e.realFactors?.rsi, macd_hist: e.realFactors?.macd?.histogram, ma50: e.realFactors?.ma50, ma200: e.realFactors?.ma200, technical_score: e.realFactors?.technical_score, momentum_score: e.realFactors?.momentum_score, risk_score: e.realFactors?.risk_score, summary: e.summary })), null, 2)}\n\nSelect the best trades to execute NOW. Risk limits: max position ${pp.max_position_pct}% of portfolio, max sector ${pp.max_sector_pct}%, min confidence ${pp.min_confidence}%, max ${pp.max_daily_trades} trades, stop-loss ${pp.stop_loss_pct}% below entry. For each proposal: symbol, company_name, sector, action (buy/sell), current_price (use the real price), confidence, target_price, stop_loss, suggested_position_pct (0-${pp.max_position_pct}), reasoning. Only sell stocks currently held. Do not exceed ${pp.max_daily_trades} trades.`,
      response_json_schema: {
        type: 'object',
        properties: {
          risk_assessment: { type: 'string' },
          proposals: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                symbol: { type: 'string' }, company_name: { type: 'string' }, sector: { type: 'string' },
                action: { type: 'string', enum: ['buy', 'sell'] }, current_price: { type: 'number' },
                confidence: { type: 'number' }, target_price: { type: 'number' }, stop_loss: { type: 'number' },
                suggested_position_pct: { type: 'number' }, reasoning: { type: 'string' },
              },
            },
          },
        },
        required: ['risk_assessment', 'proposals'],
      },
    });
    let proposals = (p2.proposals || []).filter((pr) => (pr.confidence || 0) >= pp.min_confidence).slice(0, pp.max_daily_trades);

    // PASS 3a — Investment Committee Debate (4-archetype consensus)
    if (proposals.length) {
      const committee = await sr.integrations.Core.InvokeLLM({
        model: 'claude-sonnet-5',
        prompt: `You are AlphaTrade AI's INVESTMENT COMMITTEE — 4 archetypes debate each candidate.\nProposals:\n${JSON.stringify(proposals.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector, confidence: p.confidence, reasoning: p.reasoning })), null, 2)}\nFor each, 4 archetypes (Value/Utility Specialist, Macro Contrarian, Quant Statistician, Tail-Risk Hedger) each give vote (bullish/bearish/neutral) + one-line argument + conviction (0-100). consensus_votes = count of bullish (of 4). consensus = true if >= 3 bullish. Return debates.`,
        response_json_schema: {
          type: 'object',
          properties: {
            debates: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  symbol: { type: 'string' }, consensus_votes: { type: 'number' }, consensus: { type: 'boolean' },
                },
              },
            },
          },
          required: ['debates'],
        },
      });
      const consensusMap = {};
      (committee.debates || []).forEach((d) => { consensusMap[d.symbol.toUpperCase()] = d.consensus; });
      proposals = proposals.filter((p) => consensusMap[p.symbol.toUpperCase()] !== false);
    }

    // PASS 3b — Deterministic ML multi-factor scoring (REAL indicators + self-learning weights)
    const scored = proposals.map((p) => {
      const ef = enriched.find((e) => e.symbol === p.symbol);
      const f = ef?.realFactors;
      const technical = f?.technical_score ?? 50;
      const momentum = f?.momentum_score ?? 50;
      const risk = f?.risk_score ?? 50;
      const fundamental = p.confidence ?? 50;
      const sentiment = 50;
      const ml_score = weightedComposite({ technical, fundamental, sentiment, momentum, risk }, weights);
      return { ...p, ml_score: Math.round(ml_score * 10) / 10, ml_signal: signalFromComposite(ml_score), realFactors: f };
    });
    let approved = scored.filter((p) => p.ml_score >= 45);

    // PASS 4 — Adversarial Risk Officer veto
    if (approved.length) {
      const p4 = await sr.integrations.Core.InvokeLLM({
        model: 'claude-sonnet-5',
        prompt: `You are the ADVERSARIAL RISK OFFICER. Veto dangerous trades.\nPortfolio:\n${pCtx}\nSector exposure:\n${secCtx}\n\nProposals:\n${JSON.stringify(approved.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector, confidence: p.confidence, ml_score: p.ml_score, suggested_position_pct: p.suggested_position_pct })), null, 2)}\nFor each, return verdict "approved"/"flagged"/"vetoed" + one-line note. Veto on hidden correlation, liquidity trap, concentration breach, or regime mismatch.`,
        response_json_schema: {
          type: 'object',
          properties: {
            reviews: {
              type: 'array',
              items: {
                type: 'object',
                properties: { symbol: { type: 'string' }, verdict: { type: 'string', enum: ['approved', 'flagged', 'vetoed'] }, note: { type: 'string' } },
              },
            },
          },
          required: ['reviews'],
        },
      });
      const vetoMap = {};
      (p4.reviews || []).forEach((r) => { vetoMap[r.symbol.toUpperCase()] = r.verdict; });
      approved = approved.filter((p) => (vetoMap[p.symbol.toUpperCase()] || 'approved') !== 'vetoed');
    }

    // PASS 5 — Causal Contagion (DAG) — systemic risk note per approved trade
    let contagion = null;
    if (approved.length) {
      try {
        contagion = await sr.integrations.Core.InvokeLLM({
          model: 'claude-sonnet-5',
          prompt: `You are AlphaTrade AI's CAUSAL CONTAGION ENGINE. For each approved trade, give a one-line root_cause and a contagion_risk score (0-100).\nApproved:\n${JSON.stringify(approved.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector })), null, 2)}\nPortfolio: ${holdings.map((h) => h.symbol).join(', ') || 'empty'}`,
          response_json_schema: {
            type: 'object',
            properties: {
              items: { type: 'array', items: { type: 'object', properties: { symbol: { type: 'string' }, root_cause: { type: 'string' }, contagion_risk: { type: 'number' } } } },
            },
            required: ['items'],
          },
        });
      } catch (e) { /* non-fatal */ }
    }
    // Block trades with extreme contagion risk
    if (contagion?.items) {
      const crMap = {};
      contagion.items.forEach((c) => { crMap[c.symbol.toUpperCase()] = c.contagion_risk || 0; });
      approved = approved.filter((p) => (crMap[p.symbol.toUpperCase()] || 0) < 80);
    }

    // Authoritative capital base: use broker account equity when connected, else securities market value.
    let accountEquity = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    if (user.broker === 'alpaca' && user.broker_api_key && user.broker_api_secret) {
      try {
        const acct = await getAlpacaAccount({ apiKey: user.broker_api_key, secretKey: user.broker_api_secret, mode: user.broker_mode || 'paper' });
        if (acct && Number(acct.equity) > 0) accountEquity = Number(acct.equity);
      } catch (e) { /* fall back to securities market value */ }
    }

    const executed = [];
    for (const pr of approved) {
      const price = pr.realPrice || pr.current_price;
      let shares;
      if (pr.action === 'buy') {
        const maxPos = (pp.max_position_pct / 100) * accountEquity;
        const sectorVal = sec.sectors.find((s) => s.sector === (pr.sector || 'Other'))?.value || 0;
        const sectorCap = Math.max(0, (pp.max_sector_pct / 100) * accountEquity - sectorVal);
        const aiVal = ((pr.suggested_position_pct || 5) / 100) * accountEquity;
        const positionValue = Math.min(aiVal, maxPos, sectorCap);
        // Do NOT buy when authorized notional is below the cost of one whole share.
        shares = price > 0 && positionValue >= price ? Math.floor(positionValue / price) : 0;
      } else {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        shares = existing ? existing.shares : 0;
      }
      if (shares <= 0) continue;

      const f = pr.realFactors || {};
      const result = await settleTrade(base44, user, {
        symbol: pr.symbol, action: pr.action, qty: shares, price,
        company_name: pr.company_name, sector: pr.sector, confidence: pr.confidence,
        target_price: pr.target_price, stop_loss: pr.stop_loss, ai_recommended: true,
        source: 'autonomous', reasoning: pr.reasoning, ml_score: pr.ml_score,
        technical_score: f.technical_score, momentum_score: f.momentum_score, risk_score: f.risk_score,
        recordDecision: true,
      });
      executed.push({ symbol: pr.symbol, action: pr.action, qty: shares, price, ml_score: pr.ml_score, settlement: result });
    }

    // SELF-LEARNING — adjust 5-factor weights from REALIZED trade outcomes (evidence-based)
    let newWeights = weights;
    try {
      const past = await sr.entities.AITradeDecision.list('-created_date', 30);
      const allTrades = await sr.entities.Trade.list('-created_date', 200);
      const outcomes = past.map((d) => {
        const sells = allTrades.filter(
          (t) => t.symbol === d.symbol && t.action === 'sell' && new Date(t.created_date) >= new Date(d.created_date)
        );
        let realizedPnl = null;
        if (d.action === 'buy' && sells.length) {
          realizedPnl = sells.reduce((s, t) => s + (t.price - d.price) * t.shares, 0);
        }
        return {
          symbol: d.symbol, action: d.action, ml_score: d.ml_score,
          technical_score: d.technical_score, momentum_score: d.momentum_score, risk_score: d.risk_score,
          outcome: realizedPnl === null ? 'open' : realizedPnl > 0 ? 'win' : 'loss',
          realized_pnl: realizedPnl,
        };
      });
      const labeled = outcomes.filter((o) => o.outcome !== 'open');
      if (labeled.length >= 3) {
        const sl = await sr.integrations.Core.InvokeLLM({
          model: 'claude-sonnet-5',
          prompt: `You are AlphaTrade AI's SELF-LEARNING ENGINE. Below are PAST decisions with their REALIZED outcomes (win/loss + realized P&L). Diagnose which factors predicted winners vs losers and produce ADJUSTED 5-factor weights (must sum to 100, each 5-40).\nDecisions with outcomes:\n${JSON.stringify(labeled, null, 2)}\nCurrent weights: ${JSON.stringify(weights)}. Return weights + accuracy (% of labeled that were wins) + summary.`,
          response_json_schema: {
            type: 'object',
            properties: {
              weights: { type: 'object', properties: { technical: { type: 'number' }, fundamental: { type: 'number' }, sentiment: { type: 'number' }, momentum: { type: 'number' }, risk: { type: 'number' } } },
              accuracy: { type: 'number' }, summary: { type: 'string' },
            },
          },
        });
        if (sl.weights) newWeights = sl.weights;
      }
    } catch (e) { /* non-fatal */ }
    try { await sr.entities.User.update(user.id, { ml_weights: newWeights }); } catch (e) { /* non-fatal */ }

    // Email alert
    if (executed.length) {
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse: Autonomous AI executed ${executed.length} trade(s)`,
          body: executed.map((e) => `${e.action.toUpperCase()} ${e.qty} ${e.symbol} @ $${e.price.toFixed(2)} (ML score ${e.ml_score})`).join('\n'),
        });
      } catch (e) {}
    }

    return Response.json({
      ok: true,
      market_summary: p1.market_summary,
      risk_assessment: p2.risk_assessment,
      candidates_scanned: candidates.length,
      proposals_after_fit: (p2.proposals || []).length,
      proposals_after_committee: proposals.length,
      proposals_after_veto: approved.length,
      executed,
      ml_weights: newWeights,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}