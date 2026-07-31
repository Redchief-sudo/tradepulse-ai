import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { placeAlpacaOrder } from '../../shared/alpaca.ts';

// Institutional risk profiles (mirrors src/lib/tradeProfiles.js)
const PROFILES = {
  aggressive: { max_position_pct: 15, max_sector_pct: 40, min_confidence: 70, max_daily_trades: 8, stop_loss_pct: 12 },
  balanced: { max_position_pct: 10, max_sector_pct: 25, min_confidence: 80, max_daily_trades: 5, stop_loss_pct: 8 },
  conservative: { max_position_pct: 5, max_sector_pct: 15, min_confidence: 88, max_daily_trades: 3, stop_loss_pct: 5 },
};

function profileParams(id) {
  return PROFILES[id] || PROFILES.balanced;
}

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

// Admin / scheduled autonomous AI trading cycle.
// Runs a streamlined 3-pass pipeline (Gemini scan -> Claude portfolio fit -> Claude adversarial veto),
// then auto-executes approved proposals through Alpaca (if connected) or as paper trades,
// and emails the user a summary. Designed to run on a slow schedule (e.g. daily) to conserve credits.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') {
      return Response.json({ error: 'Admin only' }, { status: 403 });
    }

    const sr = base44.asServiceRole;
    const holdings = await sr.entities.Holding.list();
    const pp = profileParams(user.trade_profile || 'balanced');
    const pCtx = portfolioContext(holdings);
    const sec = sectorExposure(holdings);
    const secCtx = sec.sectors.length ? sec.sectors.map((s) => `${s.sector}: ${s.percent.toFixed(1)}% ($${s.value.toFixed(0)})`).join(', ') : 'No sector exposure yet.';

    // PASS 1 — Multi-asset deep market scan (Gemini 3.1 Pro with web search)
    const p1 = await sr.integrations.Core.InvokeLLM({
      prompt: `You are AlphaTrade AI. PASS 1 — Multi-asset deep market scan.
Current portfolio:\n${pCtx}\n\nCurrent sector exposure:\n${secCtx}\n
Scan today's real-time US equity market and identify 5-7 high-potential candidates. For each: symbol, company_name, sector, current_price, asset_class ("stocks"), recommendation (STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL), confidence (0-100), target_price, stop_loss, and a one-line summary. Flag weak current holdings as sells. Prioritize setups that improve diversification.`,
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
                recommendation: { type: 'string', enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'] },
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

    const candidates = p1.candidates || [];
    if (!candidates.length) return Response.json({ ok: true, message: 'No candidates', market_summary: p1.market_summary });

    // PASS 2 — Portfolio fit & risk-aware sizing (Claude Sonnet 5)
    const p2 = await sr.integrations.Core.InvokeLLM({
      model: 'claude-sonnet-5',
      prompt: `You are AlphaTrade AI. PASS 2 — Portfolio fit & risk-aware selection.
Current portfolio:\n${pCtx}\n\nSector exposure:\n${secCtx}\nTotal value: $${sec.total.toFixed(0)}\n
Candidates:\n${JSON.stringify(candidates.map((c) => ({ symbol: c.symbol, sector: c.sector, current_price: c.current_price, recommendation: c.recommendation, confidence: c.confidence, target_price: c.target_price, stop_loss: c.stop_loss, summary: c.summary })), null, 2)}
Select the best trades to execute NOW. Risk limits: max position ${pp.max_position_pct}% of portfolio, max sector ${pp.max_sector_pct}%, min confidence ${pp.min_confidence}%, max ${pp.max_daily_trades} trades, stop-loss ${pp.stop_loss_pct}% below entry. For each proposal: symbol, company_name, sector, action (buy/sell), current_price, confidence, target_price, stop_loss, suggested_position_pct (0-${pp.max_position_pct}), reasoning. Only sell stocks currently held. Do not exceed ${pp.max_daily_trades} trades.`,
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
                reasoning: { type: 'string' },
              },
            },
          },
        },
        required: ['risk_assessment', 'proposals'],
      },
    });

    let proposals = (p2.proposals || []).filter((pr) => (pr.confidence || 0) >= pp.min_confidence).slice(0, pp.max_daily_trades);

    // PASS 3 — Adversarial risk veto (Claude Sonnet 5)
    const p3 = await sr.integrations.Core.InvokeLLM({
      model: 'claude-sonnet-5',
      prompt: `You are the ADVERSARIAL RISK OFFICER. Veto dangerous trades.
Portfolio:\n${pCtx}\nSector exposure:\n${secCtx}\n
Proposals:\n${JSON.stringify(proposals.map((p) => ({ symbol: p.symbol, action: p.action, sector: p.sector, confidence: p.confidence, suggested_position_pct: p.suggested_position_pct })), null, 2)}
For each, return verdict "approved" / "flagged" / "vetoed" and a one-line note. Veto on hidden correlation, liquidity trap, concentration breach, or regime mismatch.`,
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
    (p3.reviews || []).forEach((r) => { vetoMap[r.symbol.toUpperCase()] = r; });
    proposals = proposals.filter((p) => (vetoMap[p.symbol.toUpperCase()]?.verdict || 'approved') !== 'vetoed');

    // Execute approved proposals
    const portfolioValue = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    const executed = [];
    for (const pr of proposals) {
      let shares, positionValue;
      if (pr.action === 'buy') {
        const maxPos = (pp.max_position_pct / 100) * portfolioValue;
        const sectorVal = sec.sectors.find((s) => s.sector === (pr.sector || 'Other'))?.value || 0;
        const sectorCap = Math.max(0, (pp.max_sector_pct / 100) * portfolioValue - sectorVal);
        const aiVal = ((pr.suggested_position_pct || 5) / 100) * portfolioValue;
        positionValue = Math.min(aiVal, maxPos, sectorCap);
        shares = pr.current_price > 0 ? Math.max(1, Math.floor(positionValue / pr.current_price)) : 0;
        positionValue = shares * pr.current_price;
      } else {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        shares = existing ? existing.shares : 0;
        positionValue = shares * pr.current_price;
      }
      if (shares <= 0) continue;

      let brokerOrder = null;
      if (user.broker === 'alpaca' && user.broker_api_key && user.broker_api_secret) {
        try {
          brokerOrder = await placeAlpacaOrder({
            apiKey: user.broker_api_key, secretKey: user.broker_api_secret,
            mode: user.broker_mode || 'paper', symbol: pr.symbol, qty: shares, side: pr.action,
          });
        } catch (e) { brokerOrder = { error: e.message }; }
      }

      await sr.entities.Trade.create({
        symbol: pr.symbol, company_name: pr.company_name || pr.symbol, action: pr.action,
        shares, price: pr.current_price, total_value: positionValue, ai_recommended: true,
      });
      if (pr.action === 'buy') {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        if (existing) {
          const ts = existing.shares + shares;
          const tc = existing.shares * existing.avg_price + positionValue;
          await sr.entities.Holding.update(existing.id, { shares: ts, avg_price: tc / ts, current_price: pr.current_price, stop_loss: pr.stop_loss, target_price: pr.target_price });
        } else {
          await sr.entities.Holding.create({ symbol: pr.symbol, company_name: pr.company_name || pr.symbol, shares, avg_price: pr.current_price, current_price: pr.current_price, sector: pr.sector || '', day_change_percent: 0, stop_loss: pr.stop_loss, target_price: pr.target_price });
        }
      } else {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        if (existing) { const ns = existing.shares - shares; if (ns <= 0) await sr.entities.Holding.delete(existing.id); else await sr.entities.Holding.update(existing.id, { shares: ns }); }
      }

      await sr.entities.AITradeDecision.create({
        symbol: pr.symbol, company_name: pr.company_name || pr.symbol, sector: pr.sector || '',
        asset_class: 'stocks', action: pr.action, shares, price: pr.current_price,
        position_value: positionValue, confidence: pr.confidence, target_price: pr.target_price,
        stop_loss: pr.stop_loss, reasoning: pr.reasoning, status: 'executed',
      });
      executed.push({ symbol: pr.symbol, action: pr.action, shares, price: pr.current_price, brokerOrder });
    }

    // Email alert
    if (executed.length) {
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse: Autonomous AI executed ${executed.length} trade(s)`,
          body: executed.map((e) => `${e.action.toUpperCase()} ${e.shares} ${e.symbol} @ $${e.price.toFixed(2)}`).join('\n'),
        });
      } catch (e) {}
    }

    return Response.json({
      ok: true,
      market_summary: p1.market_summary,
      risk_assessment: p2.risk_assessment,
      candidates_scanned: candidates.length,
      proposals: proposals.length,
      executed,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}