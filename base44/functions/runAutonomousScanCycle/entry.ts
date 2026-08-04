import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { computeRealFactors, weightedComposite, signalFromComposite } from '../../shared/quantScore.ts';
import { classifyRegimeFromSnapshots } from '../../shared/regime.ts';
import { netEdge } from '../../shared/costModel.ts';
import { getChampion } from '../../shared/modelGovernance.ts';
import { getAlpacaAccount } from '../../shared/alpaca.ts';
import { fetchCandles as fetchMultiAssetCandles } from '../../shared/marketDataAdapter.ts';
import { sendTelegramMessage } from '../../shared/telegram.ts';

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

// Multi-asset candle fetching is handled by marketDataAdapter.ts
// (stocks via Yahoo Finance, crypto via Coinbase — no key required for either).

// Full 5-pass autonomous AI trading cycle.
// Uses CHAMPION model weights from the versioned StrategyModel registry.
// Weight evolution is handled by the separate Model Governance workflow —
// this cycle only executes trades, it does not mutate the model.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    // AUTONOMY GATE: only trade when the user has activated the session.
    // The scheduled workflows still call this function, but it no-ops until
    // the user presses "Start Trading" on the Dashboard. Stop-loss/take-profit
    // protection (runStopLossCycle) is NOT gated — it always protects positions.
    if (!user.trading_active) {
      return Response.json({ ok: true, skipped: true, reason: 'Trading not active — press Start to begin' });
    }

    const sr = base44.asServiceRole;
    const key = secrets.get('FINNHUB_API_KEY');
    // Run-level identifier for stable idempotency keys — all trades in this
    // scan cycle share the same runId, so retries resume the same intents.
    // Derive run ID from scheduled occurrence (UTC date+hour) so retries of
    // the same scheduled scan reuse the same ID — preventing duplicate orders
    // across workflow retries. Different hours get different IDs.
    const now = new Date();
    const runId = `scan-${now.getUTCFullYear()}${String(now.getUTCMonth()+1).padStart(2,'0')}${String(now.getUTCDate()).padStart(2,'0')}-${String(now.getUTCHours()).padStart(2,'0')}`;
    // Persist an authoritative ScanRun record so the Dashboard can display
    // scan state from durable data rather than transient page state.
    const scanRun = await sr.entities.ScanRun.create({
      user_id: user.id,
      scan_run_id: runId,
      started_at: now.toISOString(),
      trigger_source: 'scheduled',
      status: 'running',
      candidates_found: 0, proposals_created: 0, proposals_vetoed: 0,
      trades_attempted: 0, trades_filled: 0, trades_rejected: 0,
    });
    const finishRun = async (patch) => {
      try { await sr.entities.ScanRun.update(scanRun.id, { completed_at: new Date().toISOString(), ...patch }); } catch (e) {}
    };
    // USER-SCOPED: only this user's holdings
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const pp = profileParams(user.trade_profile || 'balanced');

    // Deterministic market regime (computed first so we can select the regime-specific champion)
    const regime = await classifyRegimeFromSnapshots(sr);

    // CHAMPION MODEL: use the regime-specific champion's weights.
    // The governance workflow promotes challengers; this cycle always uses the champion.
    const champion = await getChampion(sr, user.id, regime.market_regime);
    const weights = champion?.weights || user.ml_weights || { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };

    // Authoritative capital base: fetch broker account equity EARLY so the AI
    // passes (especially Pass 2 portfolio fit) can size proposals against real
    // capital, not a stale holdings-derived value ($0 when the portfolio is
    // empty). FAIL-CLOSED: for broker-connected users, real account equity is
    // a prerequisite — sizing from a stale holdings cache is fail-open behavior.
    const brokerCreds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    let accountEquity;
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
        if (!acct || Number(acct.equity) <= 0) {
          await finishRun({ status: 'broker_unavailable', error: 'BROKER_ACCOUNT_UNAVAILABLE', market_regime: regime.market_regime, model_version: champion?.version || 'default' });
          return Response.json({ ok: false, error: 'BROKER_ACCOUNT_UNAVAILABLE: cannot size positions without real account equity' }, { status: 503 });
        }
        accountEquity = Number(acct.equity);
      } catch (e) {
        await finishRun({ status: 'broker_unavailable', error: `BROKER_UNREACHABLE: ${e.message}`, market_regime: regime.market_regime, model_version: champion?.version || 'default' });
        return Response.json({ ok: false, error: `BROKER_UNREACHABLE: ${e.message}` }, { status: 503 });
      }
    } else {
      // No broker connected — internal_paper mode, use holdings-based equity.
      accountEquity = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    }

    const pCtx = portfolioContext(holdings);
    const sec = sectorExposure(holdings);
    const secCtx = sec.sectors.length ? sec.sectors.map((s) => `${s.sector}: ${s.percent.toFixed(1)}% ($${s.value.toFixed(0)})`).join(', ') : 'No sector exposure yet.';

    // PASS 1 — Multi-asset deep market scan (Gemini 3.1 Pro, web search)
    const p1 = await sr.integrations.Core.InvokeLLM({
      prompt: `You are AlphaTrade AI. PASS 1 — Multi-asset deep market scan.\nCurrent portfolio:\n${pCtx}\n\nSector exposure:\n${secCtx}\n\nScan today's real-time markets across ALL asset classes — US equities, cryptocurrency, forex, commodities, and fixed income — and identify 7-10 high-potential candidates. Use proper symbols for each class (AAPL for stocks, BTC-USD for crypto, EURUSD=X for forex, GC=F for gold futures, TLT for bond ETFs). For each: asset_class (stocks/crypto/forex/commodities/fixed_income), symbol, company_name, sector, current_price, recommendation (STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL), confidence (0-100), target_price, stop_loss, fundamentals (P/E and revenue growth for stocks; on-chain metrics for crypto; interest rate sensitivity for bonds; supply/demand for commodities), news_catalysts, and a one-line summary. Flag weak current holdings as sells. Prioritize setups that improve cross-asset diversification.`,
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
                asset_class: { type: 'string', enum: ['stocks', 'crypto', 'forex', 'commodities', 'fixed_income'] }, symbol: { type: 'string' }, company_name: { type: 'string' }, sector: { type: 'string' },
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
    if (!candidates.length) {
      await finishRun({ status: 'no_candidates', market_summary: p1.market_summary, market_regime: regime.market_regime, model_version: champion?.version || 'default' });
      return Response.json({ ok: true, message: 'No candidates', market_summary: p1.market_summary, champion_version: champion?.version });
    }

    // Enrich each candidate with REAL indicators
    const enriched = [];
    const candleTo = Math.floor(Date.now() / 1000);
    const candleFrom = candleTo - 220 * 86400;
    for (const c of candidates) {
      const ac = (c.asset_class || 'stocks').toLowerCase();
      const candles = await fetchMultiAssetCandles(c.symbol, ac, candleFrom, candleTo, key);
      const factors = candles ? computeRealFactors(candles) : null;
      enriched.push({ ...c, asset_class: ac, realFactors: factors, realPrice: factors?.price || c.current_price });
    }

    // PASS 2 — Portfolio fit & risk-aware sizing (Claude Sonnet 5)
    // The LLM provides a risk assessment; proposals are constructed DETERMINISTICALLY
    // from the Pass 1 candidates. The LLM's structured-array output is unreliable
    // (it consistently returns empty arrays for nested object arrays), so we build
    // proposals from candidate data directly — the candidates already carry
    // confidence, target_price, stop_loss, and recommendation from Pass 1.
    const p2 = await sr.integrations.Core.InvokeLLM({
      model: 'claude-sonnet-5',
      prompt: `You are AlphaTrade AI. PASS 2 — Portfolio fit & risk assessment.\nCurrent portfolio:\n${pCtx}\n\nSector exposure:\n${secCtx}\nTotal portfolio value: $${accountEquity.toFixed(0)} (available capital)\n\nCandidates:\n${JSON.stringify(enriched.map((e) => ({ symbol: e.symbol, sector: e.sector, current_price: e.realPrice, recommendation: e.recommendation, confidence: e.confidence, summary: e.summary })), null, 2)}\n\nProvide a BRIEF risk assessment (2-3 sentences) of these candidates for a ${user.trade_profile || 'balanced'} risk profile. Which are strongest? Any concentration or correlation concerns?`,
      response_json_schema: {
        type: 'object',
        properties: {
          risk_assessment: { type: 'string' },
        },
        required: ['risk_assessment'],
      },
    });

    // Construct proposals deterministically from candidates.
    // BUY candidates: STRONG_BUY/BUY with confidence >= min_confidence, sorted by confidence.
    // SELL candidates: SELL/STRONG_SELL for symbols currently held.
    const heldSymbols = new Set(holdings.map((h) => h.symbol.toUpperCase()));
    const buyCandidates = enriched
      .filter((e) => ['STRONG_BUY', 'BUY'].includes(e.recommendation) && (e.confidence || 0) >= pp.min_confidence)
      .filter((e) => {
        const f = e.realFactors;
        if (!f) return true;
        // SMARTER ENTRIES — don't chase overbought or overextended stocks
        if (f.rsi != null && f.rsi > 72) return false;              // RSI > 72 = overbought, wait for pullback
        if (f.ma50 != null && f.price > f.ma50 * 1.07) return false; // > 7% above SMA50 = chasing
        if (f.technical_score < 52) return false;                    // require technical confirmation
        return true;
      })
      .sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    const sellCandidates = enriched
      .filter((e) => ['STRONG_SELL', 'SELL'].includes(e.recommendation) && heldSymbols.has(String(e.symbol).toUpperCase()));

    let proposals = [];
    for (const e of buyCandidates) {
      if (proposals.length >= pp.max_daily_trades) break;
      const price = e.realPrice || e.current_price || 0;
      if (price <= 0) continue;
      // Confidence-weighted position sizing: scale from 50% to 100% of max
      // position pct based on confidence (min_confidence → 50%, 100% → 100%).
      const confRatio = Math.min(1, Math.max(0.5, (e.confidence || pp.min_confidence) / 100));
      // ATR-based risk levels — adapt to each stock's actual volatility.
      // Stop = 1.5x ATR below entry (tighter in calm markets, wider in volatile ones).
      // Target = 2.5x ATR above entry (realistic take-profit based on volatility).
      const atrVal = e.realFactors?.atr;
      const stopLoss = (atrVal && atrVal > 0)
        ? Math.round((price - 1.5 * atrVal) * 100) / 100
        : (e.stop_loss || price * (1 - pp.stop_loss_pct / 100));
      const targetPrice = (atrVal && atrVal > 0)
        ? Math.round((price + 2.5 * atrVal) * 100) / 100
        : (e.target_price || price * 1.15);
      proposals.push({
        symbol: e.symbol,
        company_name: e.company_name || e.symbol,
        sector: e.sector || 'Other',
        asset_class: e.asset_class || 'stocks',
        action: 'buy',
        current_price: price,
        confidence: e.confidence || pp.min_confidence,
        target_price: targetPrice,
        stop_loss: stopLoss,
        suggested_position_pct: Math.round(pp.max_position_pct * confRatio * 10) / 10,
        reasoning: e.summary || `${e.symbol} buy signal at ${e.confidence}% confidence`,
        realFactors: e.realFactors,
        realPrice: e.realPrice,
      });
    }
    for (const e of sellCandidates) {
      if (proposals.length >= pp.max_daily_trades) break;
      const existing = holdings.find((h) => h.symbol === e.symbol);
      if (!existing) continue;
      proposals.push({
        symbol: e.symbol,
        company_name: e.company_name || e.symbol,
        sector: e.sector || 'Other',
        asset_class: e.asset_class || 'stocks',
        action: 'sell',
        current_price: e.realPrice || e.current_price || 0,
        confidence: e.confidence || pp.min_confidence,
        target_price: e.target_price,
        stop_loss: e.stop_loss,
        suggested_position_pct: 0,
        reasoning: e.summary || `${e.symbol} sell signal`,
        realFactors: e.realFactors,
        realPrice: e.realPrice,
      });
    }

    // PASS 3a — Investment Committee Debate
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
                properties: { symbol: { type: 'string' }, consensus_votes: { type: 'number' }, consensus: { type: 'boolean' } },
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

    // PASS 3b — Deterministic ML multi-factor scoring (CHAMPION weights)
    const scored = proposals.map((p) => {
      const ef = enriched.find((e) => e.symbol === p.symbol);
      const f = ef?.realFactors;
      const technical = f?.technical_score ?? 50;
      const momentum = f?.momentum_score ?? 50;
      const risk = f?.risk_score ?? 50;
      const fundamental = p.confidence ?? 50;
      const sentiment = 50;
      const ml_score = weightedComposite({ technical, fundamental, sentiment, momentum, risk }, weights);
      return { ...p, ml_score: Math.round(ml_score * 10) / 10, ml_signal: signalFromComposite(ml_score), realFactors: f, realPrice: ef?.realPrice };
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

    // PASS 5 — Causal Contagion
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
    if (contagion?.items) {
      const crMap = {};
      contagion.items.forEach((c) => { crMap[c.symbol.toUpperCase()] = c.contagion_risk || 0; });
      approved = approved.filter((p) => (crMap[p.symbol.toUpperCase()] || 0) < 80);
    }

    // Broker account equity was fetched early (before Pass 2) so the AI could
    // size proposals against real capital. accountEquity is already defined.

    // DAILY LOSS CIRCUIT BREAKER — block new buys after 3 losing exits or 2%
    // portfolio drawdown today. Sells still go through (risk management always runs).
    // This caps daily losses without reducing aggressiveness on good days.
    const dayStart = new Date();
    dayStart.setHours(0, 0, 0, 0);
    const recentTrades = await sr.entities.Trade.filter({ user_id: user.id });
    const todayLosses = recentTrades.filter((t) =>
      t.action === 'sell' && (t.realized_pnl || 0) < 0 && new Date(t.created_date) >= dayStart
    );
    const lossCount = todayLosses.length;
    const totalLossAmount = todayLosses.reduce((s, t) => s + (t.realized_pnl || 0), 0);
    const dailyLossPct = accountEquity > 0 ? (Math.abs(totalLossAmount) / accountEquity) * 100 : 0;
    const circuitBreakerTripped = lossCount >= 3 || dailyLossPct >= 2;

    const proposalsBeforeVeto = proposals.length;
    const executed = [];
    let tradesRejected = 0;
    for (const pr of approved) {
      // Circuit breaker — block new buys, allow sells (risk management always runs)
      if (pr.action === 'buy' && circuitBreakerTripped) continue;
      const price = pr.realPrice || pr.current_price;
      let shares;
      if (pr.action === 'buy') {
        if (regime.position_multiplier <= 0) continue;
        const grossReturnPct = pr.target_price && price > 0 ? ((pr.target_price - price) / price) * 100 : null;
        if (grossReturnPct !== null && netEdge(grossReturnPct, pr.asset_class || 'stocks') <= 0) continue;
        const maxPos = (pp.max_position_pct / 100) * accountEquity;
        const sectorVal = sec.sectors.find((s) => s.sector === (pr.sector || 'Other'))?.value || 0;
        const sectorCap = Math.max(0, (pp.max_sector_pct / 100) * accountEquity - sectorVal);
        const aiVal = ((pr.suggested_position_pct || 5) / 100) * accountEquity;
        const positionValue = Math.min(aiVal, maxPos, sectorCap) * regime.position_multiplier;
        shares = price > 0 && positionValue >= price ? Math.floor(positionValue / price) : 0;
      } else {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        shares = existing ? existing.shares : 0;
      }
      if (shares <= 0) continue;

      const f = pr.realFactors || {};
      const result = await settleTrade(base44, user, {
        symbol: pr.symbol, action: pr.action, qty: shares, price,
        company_name: pr.company_name, sector: pr.sector, asset_class: pr.asset_class || 'stocks', confidence: pr.confidence,
        target_price: pr.target_price, stop_loss: pr.stop_loss, ai_recommended: true,
        source: 'autonomous', reasoning: pr.reasoning, ml_score: pr.ml_score,
        technical_score: f.technical_score, momentum_score: f.momentum_score, risk_score: f.risk_score,
        recordDecision: true,
        regime: regime.market_regime,
        // STABLE IDEMPOTENCY: per-run + per-signal identity. The runId is generated
        // once per scan cycle, so retries of the same run reuse the same key (no
        // duplicate orders). Different signals within the same run get distinct keys.
        idempotency_key: `autonomous-${runId}-${pr.symbol}-${pr.action}`,
        signal_timestamp: new Date().toISOString(),
        finnhub_key: key,
      });
      const rStatus = result?.status || result?.settlement?.status;
      if (rStatus === 'rejected' || rStatus === 'failed' || rStatus === 'canceled') tradesRejected++;
      executed.push({ symbol: pr.symbol, action: pr.action, qty: shares, price, ml_score: pr.ml_score, settlement: result });
    }

    // NOTE: Self-learning weight evolution is now handled by the separate
    // Model Governance workflow (runModelGovernance). This cycle only executes
    // trades using the champion model's weights — it does NOT mutate the model.

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

    // Telegram alert
    if (executed.length && user.telegram_chat_id && user.telegram_notifications_enabled) {
      try {
        const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
        if (botToken) {
          const lines = executed.map((e) => `${e.action.toUpperCase()} ${e.qty} ${e.symbol} @ $${e.price.toFixed(2)} (ML ${e.ml_score})`);
          await sendTelegramMessage(
            botToken,
            String(user.telegram_chat_id),
            `🤖 <b>TradePulse AI</b> executed ${executed.length} trade(s):\n${lines.join('\n')}`
          );
        }
      } catch (e) { /* non-fatal */ }
    }

    await finishRun({
      status: 'completed',
      market_summary: p1.market_summary,
      market_regime: regime.market_regime,
      model_version: champion?.version || 'default',
      candidates_found: candidates.length,
      proposals_created: proposalsBeforeVeto,
      proposals_vetoed: Math.max(0, proposalsBeforeVeto - approved.length),
      trades_attempted: executed.length,
      trades_filled: executed.length - tradesRejected,
      trades_rejected: tradesRejected,
    });

    return Response.json({
      ok: true,
      market_summary: p1.market_summary,
      risk_assessment: p2.risk_assessment,
      regime,
      champion_version: champion?.version || 'default',
      candidates_scanned: candidates.length,
      proposals_after_fit: proposals.length,
      proposals_after_committee: proposals.length,
      proposals_after_veto: approved.length,
      executed,
      ml_weights: weights,
    });
  } catch (error) {
    try { await finishRun({ status: 'failed', error: error.message }); } catch (e) {}
    return Response.json({ error: error.message }, { status: 500 });
  }
}