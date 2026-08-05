import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { computeRealFactors, weightedComposite, signalFromComposite } from '../../shared/quantScore.ts';
import { classifyRegimeFromSnapshots } from '../../shared/regime.ts';
import { netEdge } from '../../shared/costModel.ts';
import { getChampion } from '../../shared/modelGovernance.ts';
import { getAlpacaAccount, getAlpacaClock, cancelAlpacaOrder } from '../../shared/alpaca.ts';
import { fetchCandles as fetchMultiAssetCandles } from '../../shared/marketDataAdapter.ts';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { isExecutable } from '../../shared/executableUniverse.ts';
import { riskLimitsForProfile } from '../../shared/riskEngine.ts';
import { getPaperEquity } from '../../shared/cashLedger.ts';
import { updateSessionState, SESSION_STATES } from '../../shared/sessionState.ts';

// Risk limits are defined in ONE place: riskEngine.ts. The scan cycle, execution
// gateway, and risk engine all use riskLimitsForProfile() so they never disagree.
// (Fixes Rev.12 #10: scan and execution had inconsistent risk profiles — the
// scan cycle allowed 3% daily loss for balanced while the risk engine blocked
// at 1%. Now both layers use the same authoritative limits.)
function profileParams(id) { return riskLimitsForProfile(id); }

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
  // Scan run state — declared outside the try block so the catch handler can
  // finalize a failed scan run. (Fixes Rev.9 defect #1: finishRun was declared
  // inside the try, so the catch's call to it threw a ReferenceError that was
  // silently swallowed — leaving crashed scans permanently marked "running".)
  let scanRunId = null;
  let srRef = null;
  let lockOwnerToken = null;
  let userIdRef = null;

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
    srRef = sr;
    userIdRef = user.id;
    const key = secrets.get('FINNHUB_API_KEY');

    // Trigger source — passed from the caller (dashboard/manual) or defaults
    // to 'scheduled' for workflow-triggered runs. (Fixes Rev.9 defect #17:
    // every ScanRun was hardcoded as 'scheduled' even for manual/dashboard runs.)
    const body = await req.json().catch(() => ({}));
    const triggerSource = body.trigger_source || 'scheduled';

    // Run-level identifier — includes the 15-minute occurrence slot so scans
    // within the same hour get distinct IDs. (Fixes Rev.9 defect #2: four scans
    // per hour shared one hourly ID, causing cross-scan idempotency collisions
    // and duplicate ScanRun records.) Retries of the same 15-min slot reuse
    // the same ID; the next slot gets a new one.
    const now = new Date();
    const minuteSlot = Math.floor(now.getUTCMinutes() / 15) * 15;
    const runId = `scan-${now.getUTCFullYear()}${String(now.getUTCMonth()+1).padStart(2,'0')}${String(now.getUTCDate()).padStart(2,'0')}-${String(now.getUTCHours()).padStart(2,'0')}${String(minuteSlot).padStart(2,'0')}`;

    // SCAN LOCK — dedicated ScanLock entity with owner token. (Fixes Rev.12 #1:
    // the old filter+create on ScanRun was not truly atomic — two workers could
    // both pass the filter and both create. The ScanLock approach narrows the
    // race to the create round-trip and resolves by earliest acquired_at.)
    //
    // 1) Create a ScanLock with a unique owner_token.
    // 2) Query all active (non-expired) ScanLocks for this lock_key.
    // 3) If our lock has the earliest acquired_at, we won — proceed.
    // 4) If another lock is older, we lost — delete ours and abort.
    // 5) Clean up stale expired locks from previous runs.
    lockOwnerToken = crypto.randomUUID();
    const lockKey = `scan-${user.id}-${runId}`;
    const lockExpiry = new Date(Date.now() + 3 * 60 * 1000).toISOString(); // 3 min TTL
    await sr.entities.ScanLock.create({
      user_id: user.id,
      lock_key: lockKey,
      owner_token: lockOwnerToken,
      acquired_at: now.toISOString(),
      expires_at: lockExpiry,
      heartbeat_at: now.toISOString(),
    });

    // Check for competing locks — the earliest acquired_at wins
    const competingLocks = await sr.entities.ScanLock.filter({ user_id: user.id, lock_key: lockKey });
    const activeLocks = competingLocks.filter((l) => new Date(l.expires_at).getTime() > Date.now());
    if (activeLocks.length > 1) {
      const sorted = activeLocks.sort((a, b) => new Date(a.acquired_at) - new Date(b.acquired_at));
      const winner = sorted[0];
      if (winner.owner_token !== lockOwnerToken) {
        // We lost the race — delete our lock and abort
        const ourLock = activeLocks.find((l) => l.owner_token === lockOwnerToken);
        if (ourLock) try { await sr.entities.ScanLock.delete(ourLock.id); } catch (e) {}
        return Response.json({ ok: true, skipped: true, reason: 'Lost scan lock race — another scan claimed this slot' });
      }
    }

    // Clean up stale expired locks from previous runs
    const allLocks = await sr.entities.ScanLock.filter({ user_id: user.id });
    const staleLocks = allLocks.filter((l) => new Date(l.expires_at).getTime() <= Date.now());
    for (const sl of staleLocks) {
      try { await sr.entities.ScanLock.delete(sl.id); } catch (e) {}
    }

    // Persist an authoritative ScanRun record so the Dashboard can display
    // scan state from durable data rather than transient page state.
    const scanRun = await sr.entities.ScanRun.create({
      user_id: user.id,
      scan_run_id: runId,
      started_at: now.toISOString(),
      last_heartbeat_at: now.toISOString(),
      trigger_source: triggerSource,
      status: 'running',
      candidates_found: 0, proposals_created: 0, proposals_vetoed: 0,
      trades_attempted: 0, trades_filled: 0, trades_rejected: 0,
    });
    scanRunId = scanRun.id;

    // Heartbeat — updates both ScanRun and ScanLock. Counts failures and
    // stops after 3 consecutive failures, transitioning to system_degraded.
    // (Fixes Rev.12 #3: heartbeat failures were silently ignored, allowing a
    // scan to continue while appearing stale to other workers.)
    let heartbeatFailures = 0;
    const heartbeat = async () => {
      try {
        await sr.entities.ScanRun.update(scanRun.id, { last_heartbeat_at: new Date().toISOString() });
        // Also renew the ScanLock heartbeat and expiry
        const ourLocks = await sr.entities.ScanLock.filter({ user_id: user.id, owner_token: lockOwnerToken });
        if (ourLocks[0]) {
          await sr.entities.ScanLock.update(ourLocks[0].id, {
            heartbeat_at: new Date().toISOString(),
            expires_at: new Date(Date.now() + 3 * 60 * 1000).toISOString(),
          });
        }
        heartbeatFailures = 0;
      } catch (e) {
        heartbeatFailures++;
        try {
          await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'heartbeat_failure', severity: 'warning', message: `Heartbeat persistence failed (${heartbeatFailures}/3): ${e.message}` });
        } catch (ae) {}
        if (heartbeatFailures >= 3) {
          try { await updateSessionState(sr, user.id, SESSION_STATES.SYSTEM_DEGRADED, `Heartbeat persistence failed ${heartbeatFailures} times`); } catch (se) {}
          throw new Error('HEARTBEAT_PERSISTENCE_FAILED: scan cannot continue without reliable heartbeats');
        }
      }
    };

    // Finish run — if the update fails, record an AuditEvent instead of
    // silently swallowing. (Fixes Rev.10 defect #3: finishRun silently
    // swallowed update failures, leaving scans stuck as 'running'.)
    const finishRun = async (patch) => {
      try {
        await sr.entities.ScanRun.update(scanRun.id, { completed_at: new Date().toISOString(), last_heartbeat_at: new Date().toISOString(), ...patch });
      } catch (e) {
        try {
          await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'scan_finalization_failed', severity: 'error', entity_type: 'ScanRun', entity_id: scanRun.id, message: `ScanRun update failed: ${e.message}` });
        } catch (e2) { /* audit itself failed */ }
      }
      // Release the scan lock
      try {
        const ourLocks = await sr.entities.ScanLock.filter({ user_id: user.id, owner_token: lockOwnerToken });
        for (const l of ourLocks) try { await sr.entities.ScanLock.delete(l.id); } catch (e) {}
      } catch (e) { /* non-fatal */ }
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
    let startOfDayEquity = null;
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
        if (!acct || Number(acct.equity) <= 0) {
          await finishRun({ status: 'broker_unavailable', error: 'BROKER_ACCOUNT_UNAVAILABLE', market_regime: regime.market_regime, model_version: champion?.version || 'default' });
          return Response.json({ ok: false, error: 'BROKER_ACCOUNT_UNAVAILABLE: cannot size positions without real account equity' }, { status: 503 });
        }
        accountEquity = Number(acct.equity);
        // Alpaca's last_equity is the equity at the last market close —
        // effectively the start-of-day equity. Used for the daily loss
        // circuit breaker. (Fixes Rev.9 defect #13.)
        startOfDayEquity = Number(acct.last_equity) || accountEquity;
        // Fetch the Alpaca market clock — authoritative market session state.
        // Used to set the session state to market_closed when the market is
        // closed, rather than relying on local time. (Per the audit: use
        // Alpaca's clock endpoint as the authority for stock-market session.)
        try {
          const clock = await getAlpacaClock({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
          if (clock && !clock.is_open) {
            await updateSessionState(sr, user.id, SESSION_STATES.MARKET_CLOSED, 'Alpaca clock reports market closed');
          }
        } catch (e) { /* non-fatal — fall back to local time gate in execution */ }
      } catch (e) {
        await finishRun({ status: 'broker_unavailable', error: `BROKER_UNREACHABLE: ${e.message}`, market_regime: regime.market_regime, model_version: champion?.version || 'default' });
        return Response.json({ ok: false, error: `BROKER_UNREACHABLE: ${e.message}` }, { status: 503 });
      }
    } else {
      // No broker connected — internal_paper mode, use cash + holdings equity.
      // (Fixes Rev.12 #8: the old code used only holdings value, so an empty
      // paper account had $0 capital for sizing even though the cash ledger
      // defaults to $100,000. Now equity = cash balance + position market value.)
      const holdingsValue = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
      accountEquity = await getPaperEquity(sr, user.id, holdingsValue);
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
    await heartbeat();
    // EXECUTABLE UNIVERSE FILTER — only execute trades for symbols in the
    // fixed liquid universe. Candidates outside the universe are research-
    // only: they can be analyzed but never auto-executed. (Per the audit:
    // restrict execution to a small liquid stock universe to reduce
    // spread/slippage risk and make failures easier to diagnose.)
    const allCandidates = p1.candidates || [];
    const candidates = allCandidates.filter((c) => isExecutable(c.symbol));
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

    await heartbeat();
    // Construct proposals deterministically from candidates.
    // BUY candidates: STRONG_BUY/BUY with confidence >= min_confidence, sorted by confidence.
    // SELL candidates: SELL/STRONG_SELL for symbols currently held.
    const heldSymbols = new Set(holdings.map((h) => h.symbol.toUpperCase()));
    const buyCandidates = enriched
      .filter((e) => ['STRONG_BUY', 'BUY'].includes(e.recommendation) && (e.confidence || 0) >= pp.min_confidence)
      .filter((e) => {
        const f = e.realFactors;
        if (!f) return true;
        // SELECTIVE ENTRIES — multi-factor quality gate. Only act on setups
        // with strong technical confirmation AND momentum alignment.
        if (f.rsi != null && f.rsi > 72) return false;              // RSI > 72 = overbought, wait for pullback
        if (f.rsi != null && f.rsi < 30) return false;              // RSI < 30 = catching falling knife
        if (f.ma50 != null && f.price > f.ma50 * 1.07) return false; // > 7% above SMA50 = chasing
        if (f.technical_score < 55) return false;                    // require strong technical confirmation
        if (f.momentum_score != null && f.momentum_score < 50) return false; // require momentum alignment
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
      // FAIL-CLOSED: only pass proposals with explicit consensus (true).
      // Missing/undefined reviews are rejected, not passed. (Fixes Rev.9
      // defect #4: the old `!== false` let undefined pass as approved.)
      proposals = proposals.filter((p) => consensusMap[p.symbol.toUpperCase()] === true);
    }
    await heartbeat();

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
    await heartbeat();
    let approved = scored.filter((p) => p.ml_score >= 55);

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
      // FAIL-CLOSED: only pass proposals with an explicit "approved" verdict.
      // Missing/undefined reviews are rejected. (Fixes Rev.9 defect #4: the
      // old `|| 'approved'` let absent reviews pass as approved.)
      approved = approved.filter((p) => vetoMap[p.symbol.toUpperCase()] === 'approved');
    }
    await heartbeat();

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
      } catch (e) {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'contagion_pass_failed', severity: 'warning', message: `Causal contagion pass failed: ${e.message}` });
      }
    }
    if (contagion?.items) {
      const crMap = {};
      contagion.items.forEach((c) => { crMap[c.symbol.toUpperCase()] = c.contagion_risk || 0; });
      approved = approved.filter((p) => (crMap[p.symbol.toUpperCase()] || 0) < 80);
    }

    await heartbeat();
    // Broker account equity was fetched early (before Pass 2) so the AI could
    // size proposals against real capital. accountEquity is already defined.

    // DAILY LOSS CIRCUIT BREAKER — two deliberately separate policies, not
    // layered OR conditions. For broker-connected users, ONLY broker equity
    // drawdown is checked (authoritative — captures unrealized + broker-side).
    // For internal paper mode, ONLY app-recorded realized losses are checked.
    // A broker-connected user is never blocked by a stale app ledger; an
    // internal-paper user is never blocked by a broker equity fetch they don't
    // have. Sells always go through (risk management always runs).
    // (Fixes Rev.9 defect #13: the old code ran BOTH checks for broker users,
    // blocking on a stale app ledger even when broker equity was stable.)
    let circuitBreakerTripped = false;
    if (startOfDayEquity && accountEquity) {
      // Broker-connected: broker_equity_drawdown_limit only.
      const equityDeclinePct = startOfDayEquity > 0
        ? ((startOfDayEquity - accountEquity) / startOfDayEquity) * 100
        : 0;
      circuitBreakerTripped = equityDeclinePct >= pp.max_daily_loss_pct;
    } else {
      // Internal paper mode: consecutive_loss_limit + realized_loss_limit only.
      const dayStart = new Date();
      dayStart.setHours(0, 0, 0, 0);
      const recentTrades = await sr.entities.Trade.filter({ user_id: user.id });
      const todayLosses = recentTrades.filter((t) =>
        t.action === 'sell' && (t.realized_pnl || 0) < 0 && new Date(t.created_date) >= dayStart
      );
      const lossCount = todayLosses.length;
      const totalLossAmount = todayLosses.reduce((s, t) => s + (t.realized_pnl || 0), 0);
      const dailyLossPct = accountEquity > 0 ? (Math.abs(totalLossAmount) / accountEquity) * 100 : 0;
      circuitBreakerTripped = lossCount >= 3 || dailyLossPct >= pp.max_daily_loss_pct;
    }

    // KILL SWITCH — when the daily loss circuit breaker trips, cancel all
    // unfilled entry orders, set the session state to risk_stopped, persist
    // the reason, and send an alert. The system does NOT auto-re-enable —
    // the user must manually reset. (Per the audit: block new buys, cancel
    // unfilled entry orders, continue protective sells only, mark session
    // risk-stopped, send alert, require manual reset.)
    if (circuitBreakerTripped) {
      // FAIL-CLOSED: try to persist the kill switch state. If the update fails,
      // try a direct User update as fallback. If both fail, throw to abort the
      // scan — never continue with an unpersisted kill switch. (Fixes Rev.12 #25:
      // session-state persistence could fail silently, leaving trading_active
      // true after a kill switch.)
      try {
        await updateSessionState(sr, user.id, SESSION_STATES.RISK_STOPPED, `DAILY_LOSS_LIMIT: ${pp.max_daily_loss_pct}% drawdown reached`);
      } catch (e) {
        try {
          await sr.entities.User.update(user.id, { trading_active: false, kill_switch_reset_required: true, kill_switch_reason: `DAILY_LOSS_LIMIT: ${pp.max_daily_loss_pct}%`, kill_switch_at: new Date().toISOString() });
        } catch (e2) {
          try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'kill_switch_persistence_failed', severity: 'critical', message: `Both state update and fallback failed: ${e.message}; ${e2.message}` }); } catch (ae) {}
          throw new Error(`KILL_SWITCH_PERSISTENCE_FAILED: ${e.message}`);
        }
      }
      try {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'daily_loss_breach', severity: 'critical', message: `Daily loss limit reached (${pp.max_daily_loss_pct}%). Kill switch activated — new buys blocked, pending orders canceled.` });
      } catch (e) {}
      // Cancel unfilled entry (buy) orders via Alpaca
      if (brokerCreds[0]?.broker === 'alpaca') {
        try {
          const pendingIntents = await sr.entities.TradeIntent.filter({ user_id: user.id, side: 'buy' });
          const pending = pendingIntents.filter((i) => ['submitted', 'accepted'].includes(i.status));
          for (const intent of pending) {
            if (intent.broker_order_id) {
              try {
                await cancelAlpacaOrder({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode }, intent.broker_order_id);
                await sr.entities.TradeIntent.update(intent.id, { status: 'canceled', rejection_reason: 'KILL_SWITCH_CANCELED', broker_terminal_status: 'canceled' });
              } catch (e) { /* order may already be terminal */ }
            }
          }
        } catch (e) { /* non-fatal */ }
      }
      // Send alert
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: 'TradePulse KILL SWITCH: Daily loss limit reached',
          body: `The daily loss circuit breaker has tripped. All new buys are blocked and pending orders have been canceled.\n\nDaily loss limit: ${pp.max_daily_loss_pct}%\n\nTrading will remain stopped until you manually reset it from the dashboard.`,
        });
      } catch (e) {
        try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', message: `Kill switch email failed: ${e.message}` }); } catch (ae) {}
      }
    }

    // DYNAMIC SIZING — compute recent win rate from the last 20 sell trades.
    // Position size scales with conviction AND recent performance: bet bigger
    // on high-conviction A+ setups when recent trades are winning, smaller
    // when the system is cold. (Per user request: smarter dynamic sizing.)
    const recentSells = await sr.entities.Trade.filter({ user_id: user.id, action: 'sell' }, '-created_date', 20);
    const recentWinRate = recentSells.length > 0
      ? recentSells.filter((t) => (t.realized_pnl || 0) > 0).length / recentSells.length
      : 0.5; // default 50% when no history — neutral starting point

    const proposalsBeforeVeto = proposals.length;
    const executed = [];
    let tradesRejected = 0;

    // EXECUTION CAPABILITY GATE — verify the asset class is supported by the
    // connected broker before attempting execution. Unsupported candidates
    // (e.g. forex/commodities/fixed_income on Alpaca) are skipped. (Fixes
    // Rev.9 defect #5: scan could analyze an asset then attempt to execute
    // an unsupported broker symbol.)
    const brokerName = brokerCreds[0]?.broker;
    // FAIL-CLOSED: only Alpaca has an implemented execution adapter. Unknown
    // or unimplemented brokers (IBKR, TradeStation, custom) reject all asset
    // classes — candidates are research-only, never executed. (Fixes Rev.10
    // defect #9: unknown brokers failed open, allowing candidates through the
    // capability gate only to fail later or route incorrectly.)
    const isAssetClassSupported = (ac) => {
      if (!brokerName) return true; // no broker connected — internal paper mode
      if (brokerName === 'alpaca') return ['stocks', 'crypto'].includes(ac);
      return false; // IBKR, TradeStation, custom — not implemented
    };

    for (const pr of approved) {
      // Capability gate — skip unsupported asset classes (research-only)
      if (!isAssetClassSupported(pr.asset_class || 'stocks')) {
        tradesRejected++;
        continue;
      }
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
        // DYNAMIC SIZING — scale position by conviction AND recent win rate.
        // conviction_factor: 0.5 at min_confidence → 1.0 at 100% confidence.
        // win_rate_factor: 0.7 when cold → 1.0 when hot. Combined, an A+
        // setup during a hot streak gets full size; a marginal setup when
        // cold gets ~35% of base size.
        const convictionFactor = 0.5 + 0.5 * Math.min(1, Math.max(0, (pr.confidence - pp.min_confidence) / Math.max(1, 100 - pp.min_confidence)));
        const winRateFactor = 0.7 + 0.3 * recentWinRate;
        const aiVal = ((pr.suggested_position_pct || 5) / 100) * accountEquity * convictionFactor * winRateFactor;
        const positionValue = Math.min(aiVal, maxPos, sectorCap) * regime.position_multiplier;
        // FRACTIONAL SHARES — round to 0.001 so a $100 account can buy 0.035
        // shares of a $200 stock. Alpaca accepts fractional quantities.
        shares = price > 0 && positionValue > 0 ? Math.round((positionValue / price) * 1000) / 1000 : 0;
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

    // Only count and notify about actually FILLED trades — not pending or
    // rejected attempts. (Fixes Rev.9 defects #15 + #16: persisted fill count
    // was inflated by counting non-rejected as filled; notifications mislabeled
    // attempts as executions.)
    const filledTrades = executed.filter((e) => {
      const s = e.settlement?.status;
      return s === 'filled' || s === 'paper_filled';
    });

    if (filledTrades.length) {
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse: Autonomous AI executed ${filledTrades.length} trade(s)`,
          body: filledTrades.map((e) => `${e.action.toUpperCase()} ${e.qty} ${e.symbol} @ $${e.price.toFixed(2)} (ML score ${e.ml_score})`).join('\n'),
        });
      } catch (e) {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', entity_type: 'Trade', message: `Trade email notification failed: ${e.message}` });
      }

      if (user.telegram_chat_id && user.telegram_notifications_enabled) {
        try {
          const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
          if (botToken) {
            const lines = filledTrades.map((e) => `${e.action.toUpperCase()} ${e.qty} ${e.symbol} @ $${e.price.toFixed(2)} (ML ${e.ml_score})`);
            await sendTelegramMessage(
              botToken,
              String(user.telegram_chat_id),
              `🤖 <b>TradePulse AI</b> executed ${filledTrades.length} trade(s):\n${lines.join('\n')}`
            );
          }
        } catch (e) {
          await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', entity_type: 'Trade', message: `Trade Telegram notification failed: ${e.message}` });
        }
      }
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
      trades_filled: filledTrades.length,
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
    // Finalize the scan run as failed. scanRunId and srRef are at the function
    // scope so this catch block can access them. (Fixes Rev.9 defect #1.)
    if (scanRunId && srRef) {
      try { await srRef.entities.ScanRun.update(scanRunId, { completed_at: new Date().toISOString(), status: 'failed', error: error.message }); } catch (e) {}
    }
    // Release the scan lock on failure
    if (lockOwnerToken && userIdRef && srRef) {
      try {
        const locks = await srRef.entities.ScanLock.filter({ user_id: userIdRef, owner_token: lockOwnerToken });
        for (const l of locks) try { await srRef.entities.ScanLock.delete(l.id); } catch (e) {}
      } catch (e) {}
    }
    return Response.json({ error: error.message }, { status: 500 });
  }
}