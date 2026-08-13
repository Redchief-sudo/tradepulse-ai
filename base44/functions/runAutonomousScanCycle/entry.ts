import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';
import { computeRealFactors, weightedComposite, signalFromComposite } from '../../shared/quantScore.ts';
import { classifyRegimeFromSnapshots } from '../../shared/regime.ts';
import { netEdge } from '../../shared/costModel.ts';
import { effectiveGovernedWeights, getChampion } from '../../shared/modelGovernance.ts';
import { getAlpacaAccount, getAlpacaClock, getAlpacaOrder, cancelAlpacaOrder } from '../../shared/alpaca.ts';
import { fetchCandlesResult as fetchMultiAssetCandlesResult, fetchQuote as fetchMultiAssetQuote } from '../../shared/marketDataAdapter.ts';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { summarizeCandidateDispositions, summarizeFillSettlement } from '../../shared/scanConservation.ts';
import { riskLimitsForProfile, checkMaxDrawdown } from '../../shared/riskEngine.ts';
import { getPaperEquity } from '../../shared/cashLedger.ts';
import { executionSessionDecision, updateSessionState, SESSION_STATES } from '../../shared/sessionState.ts';
import { isOpenBrokerOrder } from '../../shared/operationalTruth.ts';
import { nyDayStart } from '../../shared/marketHours.ts';
import { hasNewerScanGeneration, nextScanGeneration } from '../../shared/scanState.ts';
import { canonicalizeOpportunity, normalizeAssetClass } from '../../shared/opportunity.ts';
import { assetSessionDecision, canonicalAssetClass } from '../../shared/assetSessions.ts';

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
    const value = Math.abs(h.shares * (h.current_price || h.avg_price));
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
  let heartbeatIntervalRef = null;
  // Shared fatal flag — when the periodic heartbeat reaches its failure limit,
  // it sets this. Every stage boundary checks it via assertScanHealthy() so
  // the scan aborts immediately instead of continuing through long LLM calls.
  // (Fixes Rev.14 #3: the heartbeat interval caught its own fatal exception,
  // but the main scan never knew — it continued consuming credits after the
  // heartbeat had declared the scan dead.)
  let heartbeatFatalError = null;
  const assertScanHealthy = () => {
    if (heartbeatFatalError) throw heartbeatFatalError;
  };

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
    const requestIdentity = body.scan_request_id ? String(body.scan_request_id) : 'scheduled';

    // Run-level identifier — includes the 15-minute occurrence slot so scans
    // within the same hour get distinct IDs. (Fixes Rev.9 defect #2: four scans
    // per hour shared one hourly ID, causing cross-scan idempotency collisions
    // and duplicate ScanRun records.) Retries of the same 15-min slot reuse
    // the same ID; the next slot gets a new one.
    const now = new Date();
    const minuteSlot = Math.floor(now.getUTCMinutes() / 15) * 15;
    const runId = `scan-${now.getUTCFullYear()}${String(now.getUTCMonth()+1).padStart(2,'0')}${String(now.getUTCDate()).padStart(2,'0')}-${String(now.getUTCHours()).padStart(2,'0')}${String(minuteSlot).padStart(2,'0')}-${requestIdentity}`;

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
    const lockKey = `scan-${user.id}`;
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
        if (ourLock) {
          try { await sr.entities.ScanLock.delete(ourLock.id); }
          catch (e) { try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Failed to delete losing-race lock ${ourLock.id}: ${e.message}` }); } catch (ae) {} }
        }
        return Response.json({ ok: true, skipped: true, reason: 'Lost scan lock race — another scan claimed this slot' });
      }
    }

    // Clean up stale expired locks from previous runs
    const allLocks = await sr.entities.ScanLock.filter({ user_id: user.id });
    const staleLocks = allLocks.filter((l) => new Date(l.expires_at).getTime() <= Date.now());
    for (const sl of staleLocks) {
      try { await sr.entities.ScanLock.delete(sl.id); }
      catch (e) { try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Failed to delete stale lock ${sl.id}: ${e.message}` }); } catch (ae) {} }
    }

    // Persist a monotonically increasing generation. Any older running scan is
    // superseded before this generation becomes authoritative.
    const priorRuns = await sr.entities.ScanRun.filter({ user_id: user.id }, '-started_at', 200);
    const scanGeneration = nextScanGeneration(priorRuns);
    for (const prior of priorRuns.filter((candidate) => candidate.status === 'running')) {
      await sr.entities.ScanRun.update(prior.id, {
        status: 'superseded',
        completed_at: new Date().toISOString(),
        error: `SUPERSEDED_BY_GENERATION_${scanGeneration}`,
      });
    }

    // Persist an authoritative ScanRun record so the Dashboard can display
    // scan state from durable data rather than transient page state.
    const scanRun = await sr.entities.ScanRun.create({
      user_id: user.id,
      scan_run_id: runId,
      scan_generation: scanGeneration,
      started_at: now.toISOString(),
      last_heartbeat_at: now.toISOString(),
      trigger_source: triggerSource,
      status: 'running',
      candidates_found: 0, proposals_created: 0, proposals_vetoed: 0,
      trades_attempted: 0, trades_filled: 0, trades_rejected: 0,
    });
    scanRunId = scanRun.id;

    // Periodic heartbeat interval — assigned to the function-scope ref so the
    // catch block can clear it. (Fixes Rev.13 #2: heartbeats were only issued
    // at stage boundaries, so a long LLM call could exceed the 3-minute lock
    // TTL and let another scan start. This independent timer renews the lock
    // every 30 seconds regardless of stage progress.)

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
          // Set the shared fatal flag so the main scan thread can see it.
          // (Fixes Rev.14 #3: the throw was caught by the interval callback and
          // never propagated to the main scan.)
          heartbeatFatalError = new Error('HEARTBEAT_PERSISTENCE_FAILED: scan cannot continue without reliable heartbeats');
          throw heartbeatFatalError;
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
      // Stop the periodic heartbeat
      if (heartbeatIntervalRef) clearInterval(heartbeatIntervalRef);
      // Release the scan lock — audit on failure instead of silently swallowing.
      // (Fixes Rev.13 #3: lock cleanup errors were silently swallowed.)
      try {
        const ourLocks = await sr.entities.ScanLock.filter({ user_id: user.id, owner_token: lockOwnerToken });
        for (const l of ourLocks) {
          try { await sr.entities.ScanLock.delete(l.id); }
          catch (e) {
            try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Failed to delete scan lock ${l.id}: ${e.message}` }); } catch (ae) {}
          }
        }
      } catch (e) {
        try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Lock release query failed: ${e.message}` }); } catch (ae) {}
      }
    };

    // PERIODIC HEARTBEAT — independent of stage completion. Renews the ScanLock
    // every 30 seconds so a long LLM call cannot let the lock expire and let
    // another scan start. (Fixes Rev.13 #2.)
    heartbeatIntervalRef = setInterval(async () => {
      try { await heartbeat(); } catch (e) { /* heartbeat() sets heartbeatFatalError */ }
    }, 30000);

    // OWNERSHIP VERIFICATION — before every execution decision, verify we still
    // hold the scan lock. If ownership was lost (lock expired, stolen by another
    // worker, or deleted), abort immediately to prevent duplicate execution.
    // (Fixes Rev.13 #2.)
    const verifyOwnership = async () => {
      const ourLocks = await sr.entities.ScanLock.filter({ user_id: user.id, owner_token: lockOwnerToken });
      if (!ourLocks.length) throw new Error('SCAN_LOCK_LOST: ownership transferred or lock deleted');
      const lock = ourLocks[0];
      if (new Date(lock.expires_at).getTime() <= Date.now()) {
        throw new Error('SCAN_LOCK_EXPIRED: lock TTL exceeded during scan');
      }
      // SCAN GENERATION INVALIDATION — if a newer scan superseded this one,
      // discard all results and abort. LLM calls can't be canceled, but stale
      // results can be invalidated. (Per the architecture spec: optimistic
      // cancellation — verify after every external operation.)
      const scanRuns = await sr.entities.ScanRun.filter({ user_id: user.id, scan_run_id: runId });
      if (scanRuns[0] && scanRuns[0].status !== 'running') {
        throw new Error('SCAN_SUPERSEDED: scan is no longer active — discarding stale results');
      }
      const newerRuns = await sr.entities.ScanRun.filter({ user_id: user.id });
      if (hasNewerScanGeneration(newerRuns, scanGeneration)) {
        throw new Error('SCAN_GENERATION_STALE: a newer scan superseded this result');
      }
      const currentUser = await sr.entities.User.get(user.id);
      const sessionDecision = executionSessionDecision(currentUser, 'buy', 'crypto');
      if (!sessionDecision.allowed) {
        throw new Error(`SCAN_SESSION_INVALID: ${sessionDecision.reason}`);
      }
    };

    // USER-SCOPED: only this user's holdings
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const pp = profileParams(user.trade_profile || 'balanced');

    // Deterministic market regime (computed first so we can select the regime-specific champion)
    const regime = await classifyRegimeFromSnapshots(sr, user.id);

    // CHAMPION MODEL: use the regime-specific champion's weights.
    // The governance workflow promotes challengers; this cycle always uses the champion.
    const champion = await getChampion(sr, user.id, regime.market_regime);
    const weights = effectiveGovernedWeights(champion?.weights || user.ml_weights);

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
        await heartbeat();
        await verifyOwnership();
        assertScanHealthy();
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
          await heartbeat();
          await verifyOwnership();
          assertScanHealthy();
          if (clock && !clock.is_open) {
            await updateSessionState(sr, user.id, SESSION_STATES.MARKET_CLOSED, 'Alpaca clock reports market closed');
          }
        } catch (e) {
          try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'broker_clock_unavailable', severity: 'warning', message: `Alpaca clock unavailable; execution gateway remains authoritative: ${e.message}` }); } catch (auditError) { /* execution gate still applies */ }
        }
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
    await verifyOwnership();
    assertScanHealthy();
    // Canonicalize every LLM discovery against asset-native provider data.
    // Prompt output is discovery only: it cannot supply an executable price,
    // timestamp, session, identity, or execution capability.
    const allCandidates = p1.candidates || [];
    const opportunities = [];
    const executionAssetClasses = !brokerCreds[0] || brokerCreds[0].broker === 'alpaca'
      ? ['equities', 'crypto']
      : [];
    for (const candidate of allCandidates) {
      const assetClass = normalizeAssetClass(candidate.asset_class);
      const quote = assetClass === 'unsupported'
        ? { error: `unsupported asset class: ${candidate.asset_class}` }
        : await fetchMultiAssetQuote(candidate.symbol, assetClass, key);
      await heartbeat();
      await verifyOwnership();
      assertScanHealthy();
      const opportunity = canonicalizeOpportunity(candidate, quote, new Date().toISOString(), brokerCreds[0]?.mode || 'paper', executionAssetClasses);
      await sr.entities.Opportunity.create({
        user_id: user.id,
        scan_run_id: runId,
        ...opportunity,
      });
      opportunities.push({ ...candidate, ...opportunity, symbol: opportunity.native_symbol });
    }
    const candidates = opportunities.filter((candidate) =>
      (candidate.execution_capability === 'paper_executable' || candidate.execution_capability === 'live_executable')
      && assetSessionDecision(candidate.asset_class, now).allowed
    );
    await sr.entities.ScanRun.update(scanRun.id, {
      candidates_found: candidates.length,
      market_summary: p1.market_summary,
    });
    if (!candidates.length) {
      await finishRun({ status: 'no_candidates', market_summary: p1.market_summary, market_regime: regime.market_regime, model_version: champion?.version || 'default' });
      return Response.json({ ok: true, scan_run_id: runId, message: 'No executable candidates', opportunities, market_summary: p1.market_summary, champion_version: champion?.version });
    }
    const candidateDispositions = new Map();

    // Enrich each candidate with REAL indicators
    const enriched = [];
    const candleTo = Math.floor(Date.now() / 1000);
    const candleFrom = candleTo - 220 * 86400;
    for (const c of candidates) {
      const ac = c.asset_class;
      const candleResult = await fetchMultiAssetCandlesResult(c.symbol, ac, candleFrom, candleTo, key);
      await heartbeat();
      await verifyOwnership();
      assertScanHealthy();
      const factors = candleResult.candles ? computeRealFactors(candleResult.candles) : null;
      enriched.push({ ...c, asset_class: ac, realFactors: factors, marketDataError: candleResult.candles ? null : `${candleResult.error_code || 'UNKNOWN'}: ${candleResult.message || 'No candles'}`, realPrice: factors?.price || c.price });
    }

    // PASS 2 — Portfolio fit & risk-aware sizing (Claude Sonnet 5)
    // The LLM provides a risk assessment; proposals are constructed DETERMINISTICALLY
    // from the Pass 1 candidates. The LLM's structured-array output is unreliable
    // (it consistently returns empty arrays for nested object arrays), so we build
    // proposals from candidate data directly — the candidates already carry
    // confidence, target_price, stop_loss, and recommendation from Pass 1.
    const p2 = await sr.integrations.Core.InvokeLLM({
      model: 'claude_sonnet_4_6',
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
    await verifyOwnership();
    assertScanHealthy();
    // Construct proposals deterministically from candidates.
    // BUY candidates: STRONG_BUY/BUY with confidence >= min_confidence, sorted by confidence.
    // SELL candidates: SELL/STRONG_SELL for symbols currently held.
    const heldSymbols = new Set(holdings.map((h) => h.symbol.toUpperCase()));
    const buyFilterReason = (candidate) => {
      if (!['STRONG_BUY', 'BUY'].includes(candidate.recommendation)) return `recommendation_${String(candidate.recommendation || 'missing').toLowerCase()}`;
      if ((candidate.confidence || 0) < pp.min_confidence) return `confidence_below_${pp.min_confidence}`;
      const factors = candidate.realFactors;
      if (!factors) return `market_candles_unavailable:${candidate.marketDataError || 'UNKNOWN'}`;
      if (factors.rsi != null && factors.rsi > 72) return 'rsi_overbought';
      if (factors.rsi != null && factors.rsi < 30) return 'rsi_oversold_falling_knife';
      if (factors.ma50 != null && factors.price > factors.ma50 * 1.07) return 'price_extended_above_sma50';
      if (factors.technical_score < 55) return 'technical_score_below_55';
      if (factors.momentum_score != null && factors.momentum_score < 50) return 'momentum_score_below_50';
      return null;
    };
    const buyCandidates = enriched
      .filter((candidate) => buyFilterReason(candidate) === null)
      .sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    const sellCandidates = enriched
      .filter((e) => ['STRONG_SELL', 'SELL'].includes(e.recommendation) && heldSymbols.has(String(e.symbol).toUpperCase()));

    let proposals = [];
    for (const e of buyCandidates) {
      if (proposals.length >= pp.max_daily_trades) break;
      const price = e.realPrice || 0;
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
        current_price: e.realPrice || 0,
        confidence: e.confidence || pp.min_confidence,
        target_price: e.target_price,
        stop_loss: e.stop_loss,
        suggested_position_pct: 0,
        reasoning: e.summary || `${e.symbol} sell signal`,
        realFactors: e.realFactors,
        realPrice: e.realPrice,
      });
    }
    const proposedSymbols = new Set(proposals.map((proposal) => String(proposal.symbol).toUpperCase()));
    for (const candidate of enriched) {
      if (proposedSymbols.has(String(candidate.symbol).toUpperCase())) continue;
      const reason = buyFilterReason(candidate) || 'not_an_actionable_held_sell';
      candidateDispositions.set(String(candidate.symbol).toUpperCase(), `filtered:${reason}`);
      try {
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: 'candidate_filtered',
          severity: 'info',
          correlation_id: runId,
          entity_type: 'Opportunity',
          entity_id: candidate.symbol,
          message: `${candidate.symbol} filtered before proposal: ${reason}`,
          details: JSON.stringify({ symbol: candidate.symbol, reason, recommendation: candidate.recommendation || null, confidence: candidate.confidence || 0 }),
        });
      } catch (error) {
        try {
          await sr.entities.AuditEvent.create({
            user_id: user.id,
            event_type: 'candidate_filter_audit_failed',
            severity: 'warning',
            correlation_id: runId,
            entity_type: 'ScanRun',
            entity_id: scanRun.id,
            message: `Failed to persist filter reason for ${candidate.symbol}: ${error.message}`,
          });
        } catch (auditError) { /* non-fatal observability failure */ }
      }
    }
    const proposalsBeforeVeto = proposals.length;
    await sr.entities.ScanRun.update(scanRun.id, {
      proposals_created: proposalsBeforeVeto,
    });

    // PASS 3a — prompted LLM panel. The archetypes are perspectives requested
    // from one model, not independent committee members or separate agents.
    if (proposals.length) {
      const committeeInput = [...proposals];
      const committee = await sr.integrations.Core.InvokeLLM({
        model: 'claude_sonnet_4_6',
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
      for (const proposal of committeeInput) {
        if (!proposals.some((candidate) => candidate.symbol === proposal.symbol)) {
          candidateDispositions.set(String(proposal.symbol).toUpperCase(), 'vetoed:committee_no_consensus');
        }
      }
    }
    await heartbeat();
    await verifyOwnership();
    assertScanHealthy();

    // PASS 3b — deterministic multi-factor composite (champion weights).
    // The persisted ml_score field is retained as a schema contract, but this
    // calculation is a weighted formula rather than a trained ML prediction.
    const scored = proposals.map((p) => {
      const ef = enriched.find((e) => e.symbol === p.symbol);
      const f = ef?.realFactors;
      const technical = f?.technical_score ?? 50;
      const momentum = f?.momentum_score ?? 50;
      const risk = f?.risk_score ?? 50;
      const ml_score = weightedComposite({ technical, momentum, risk }, weights);
      return { ...p, ml_score: Math.round(ml_score * 10) / 10, ml_signal: signalFromComposite(ml_score), realFactors: f, realPrice: ef?.realPrice };
    });
    await heartbeat();
    await verifyOwnership();
    assertScanHealthy();
    let approved = scored.filter((p) => p.ml_score >= 55);
    for (const proposal of proposals) {
      if (!approved.some((candidate) => candidate.symbol === proposal.symbol)) {
        candidateDispositions.set(String(proposal.symbol).toUpperCase(), 'vetoed:quant_score_below_55');
      }
    }

    // PASS 4 — Adversarial Risk Officer veto
    if (approved.length) {
      const riskOfficerInput = [...approved];
      const p4 = await sr.integrations.Core.InvokeLLM({
        model: 'claude_sonnet_4_6',
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
      for (const proposal of riskOfficerInput) {
        if (!approved.some((candidate) => candidate.symbol === proposal.symbol)) {
          candidateDispositions.set(String(proposal.symbol).toUpperCase(), `vetoed:risk_officer_${vetoMap[proposal.symbol.toUpperCase()] || 'missing_review'}`);
        }
      }
    }
    await heartbeat();
    await verifyOwnership();
    assertScanHealthy();
    await sr.entities.ScanRun.update(scanRun.id, {
      proposals_vetoed: Math.max(0, proposalsBeforeVeto - approved.length),
    });

    // PASS 5 — qualitative LLM contagion assessment (not a causal graph model)
    let contagion = null;
    if (approved.length) {
      try {
        contagion = await sr.integrations.Core.InvokeLLM({
          model: 'claude_sonnet_4_6',
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
      for (const proposal of approved) {
        if ((crMap[proposal.symbol.toUpperCase()] || 0) >= 80) {
          candidateDispositions.set(String(proposal.symbol).toUpperCase(), 'vetoed:contagion_risk');
        }
      }
      approved = approved.filter((p) => (crMap[p.symbol.toUpperCase()] || 0) < 80);
    }

    await heartbeat();
    await verifyOwnership();
    assertScanHealthy();
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
      // Use NY market session midnight, not server-local midnight. (Fixes Rev.13 #20.)
      const dayStart = nyDayStart();
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
      const cancellationSummary = { targeted: 0, confirmed: 0, already_terminal: 0, failed: 0, unknown: 0 };
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
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'daily_loss_breach', severity: 'critical', message: `Daily loss limit reached (${pp.max_daily_loss_pct}%). Kill switch activated — new buys blocked and broker cancellation reconciliation started.` });
      } catch (e) {
        try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'daily_loss_breach_audit_failed', severity: 'error', message: e.message }); } catch (auditError) { /* observability exhausted */ }
      }
      // Cancel unfilled entry (buy) orders via Alpaca
      if (brokerCreds[0]?.broker === 'alpaca') {
        try {
          const pendingIntents = await sr.entities.TradeIntent.filter({ user_id: user.id, side: 'buy' });
          const pending = pendingIntents.filter(isOpenBrokerOrder);
          for (const intent of pending) {
            if (intent.broker_order_id) {
              cancellationSummary.targeted++;
              try {
                await cancelAlpacaOrder({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode }, intent.broker_order_id);
                const confirmed = await getAlpacaOrder({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode }, intent.broker_order_id);
                if (['canceled', 'expired', 'rejected', 'filled', 'done_for_day'].includes(confirmed.status)) {
                  const canceled = confirmed.status === 'canceled';
                  cancellationSummary[canceled ? 'confirmed' : 'already_terminal']++;
                  await sr.entities.TradeIntent.update(intent.id, {
                    ...(canceled ? { status: 'canceled', rejection_reason: 'KILL_SWITCH_CANCELED' } : {}),
                    broker_terminal_status: confirmed.status,
                    last_broker_update_at: new Date().toISOString(),
                  });
                } else {
                  cancellationSummary.unknown++;
                  throw new Error(`Cancellation not terminal; Alpaca status=${confirmed.status || 'unknown'}`);
                }
              } catch (e) {
                if (!String(e.message).startsWith('Cancellation not terminal')) cancellationSummary.failed++;
                try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'kill_switch_cancel_failed', severity: 'critical', correlation_id: intent.trade_intent_id, entity_type: 'TradeIntent', entity_id: intent.id, message: `Could not confirm cancellation for ${intent.symbol}: ${e.message}` }); } catch (auditError) { /* observability exhausted */ }
              }
            }
          }
          if (cancellationSummary.already_terminal > 0) {
            try {
              const reconciliation = await base44.functions.invoke('runOrderReconciliation', {});
              const reconciliationData = reconciliation?.data || reconciliation;
              if (reconciliationData?.ok === false) throw new Error(reconciliationData.error || 'Order reconciliation returned unsuccessful result');
            } catch (error) {
              try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'kill_switch_order_reconciliation_failed', severity: 'critical', message: error.message }); } catch (auditError) { /* observability exhausted */ }
            }
          }
        } catch (e) {
          try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'kill_switch_pending_order_query_failed', severity: 'critical', message: e.message }); } catch (auditError) { /* observability exhausted */ }
        }
      }
      try {
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: 'kill_switch_cancellation_summary',
          severity: cancellationSummary.failed > 0 || cancellationSummary.unknown > 0 ? 'critical' : 'warning',
          message: `Kill-switch cancellation outcome: ${cancellationSummary.confirmed} confirmed, ${cancellationSummary.already_terminal} already terminal, ${cancellationSummary.failed} failed, ${cancellationSummary.unknown} unknown of ${cancellationSummary.targeted} targeted`,
          details: JSON.stringify(cancellationSummary),
        });
      } catch (error) {
        throw new Error(`KILL_SWITCH_CANCELLATION_AUDIT_FAILED: ${error.message}`);
      }
      // Send alert
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: 'TradePulse KILL SWITCH: Daily loss limit reached',
          body: `The daily loss circuit breaker has tripped. All new buys are blocked.\n\nOrder cancellation reconciliation: ${cancellationSummary.confirmed} canceled, ${cancellationSummary.already_terminal} already terminal, ${cancellationSummary.failed} failed, ${cancellationSummary.unknown} unknown, ${cancellationSummary.targeted} targeted.\n\nDaily loss limit: ${pp.max_daily_loss_pct}%\n\nTrading will remain stopped until you manually reset it from the dashboard.`,
        });
      } catch (e) {
        try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', message: `Kill switch email failed: ${e.message}` }); } catch (ae) {}
      }
    }

    // MAX DRAWDOWN CHECK — block new buys if drawdown exceeds the profile limit.
    // (Fixes Rev.13 #15: max_drawdown_pct was defined but never enforced pre-trade.)
    let maxDrawdownBreached = false;
    try {
      const ddCheck = await checkMaxDrawdown(sr, user.id, accountEquity, pp);
      if (ddCheck.breached) {
        maxDrawdownBreached = true;
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'max_drawdown_breach', severity: 'critical', message: `Max drawdown limit reached: ${ddCheck.drawdown_pct}% (limit ${ddCheck.limit}%, peak $${ddCheck.peak_equity.toFixed(0)})` });
      }
    } catch (e) {
      maxDrawdownBreached = true;
      try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'max_drawdown_check_failed', severity: 'critical', message: `New buys blocked because drawdown state is unavailable: ${e.message}` }); } catch (auditError) { /* financial gate remains closed */ }
    }

    // DYNAMIC SIZING — compute recent win rate from the last 20 sell trades.
    // Position size scales with conviction AND recent performance: bet bigger
    // on high-conviction A+ setups when recent trades are winning, smaller
    // when the system is cold. (Per user request: smarter dynamic sizing.)
    const recentSells = await sr.entities.Trade.filter({ user_id: user.id, action: 'sell' }, '-created_date', 20);
    const recentWinRate = recentSells.length > 0
      ? recentSells.filter((t) => (t.realized_pnl || 0) > 0).length / recentSells.length
      : 0.5; // default 50% when no history — neutral starting point

    // GROWTH SCALING — as revenue increases, the AI sizes positions larger to
    // compound gains (anti-martingale). The multiplier scales from 0.8 (when
    // down) to 1.3 (when up significantly), based on recent net realized P&L
    // relative to account equity. (Per user request: bigger trades when
    // revenue increases.)
    const recentNetPnl = recentSells.reduce((s, t) => s + (t.realized_pnl || 0), 0);
    const pnlPct = accountEquity > 0 ? (recentNetPnl / accountEquity) * 100 : 0;
    const growthMultiplier = Math.max(0.8, Math.min(1.3, 1 + pnlPct * 0.02));

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
      const canonical = canonicalAssetClass(ac);
      if (!brokerName) return true; // no broker connected — internal paper mode
      if (brokerName === 'alpaca') return ['equities', 'crypto'].includes(canonical);
      return false; // IBKR, TradeStation, custom — not implemented
    };

    // OWNERSHIP VERIFICATION — before executing any trades, verify we still
    // hold the scan lock. If a long stage caused the lock to expire and another
    // worker took over, abort to prevent duplicate execution. (Fixes Rev.13 #2.)
    await verifyOwnership();

    for (const pr of approved) {
      // Capability gate — skip unsupported asset classes (research-only)
      if (!isAssetClassSupported(pr.asset_class || 'stocks')) {
        candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:broker_asset_class');
        continue;
      }
      if (!assetSessionDecision(pr.asset_class || 'stocks').allowed) {
        candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:market_session');
        continue;
      }
      // Circuit breaker — block new buys, allow sells (risk management always runs)
      if (pr.action === 'buy' && circuitBreakerTripped) {
        candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:daily_loss_limit');
        continue;
      }
      // MAX DRAWDOWN GATE — block new buys when drawdown exceeds the profile
      // limit. (Fixes Rev.13 #15: max_drawdown_pct was defined but never enforced.)
      if (pr.action === 'buy' && maxDrawdownBreached) {
        candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:max_drawdown');
        continue;
      }
      const price = pr.realPrice || pr.current_price;
      let shares;
      if (pr.action === 'buy') {
        if (regime.position_multiplier <= 0) {
          candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:regime_multiplier');
          continue;
        }
        const grossReturnPct = pr.target_price && price > 0 ? ((pr.target_price - price) / price) * 100 : null;
        if (grossReturnPct !== null && netEdge(grossReturnPct, pr.asset_class || 'stocks') <= 0) {
          candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:no_net_edge');
          continue;
        }
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
        const aiVal = ((pr.suggested_position_pct || 5) / 100) * accountEquity * convictionFactor * winRateFactor * growthMultiplier;
        const positionValue = Math.min(aiVal, maxPos, sectorCap) * regime.position_multiplier;
        // FRACTIONAL SHARES — round to 0.001 so a $100 account can buy 0.035
        // shares of a $200 stock. Alpaca accepts fractional quantities.
        shares = price > 0 && positionValue > 0 ? Math.round((positionValue / price) * 1000) / 1000 : 0;
      } else {
        const existing = holdings.find((h) => h.symbol === pr.symbol);
        shares = existing ? existing.shares : 0;
      }
      if (shares <= 0) {
        candidateDispositions.set(String(pr.symbol).toUpperCase(), 'ineligible:zero_position_size');
        continue;
      }

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
      await heartbeat();
      await verifyOwnership();
      assertScanHealthy();
      const rStatus = result?.status || result?.settlement?.status;
      if (rStatus === 'rejected' || rStatus === 'failed' || rStatus === 'canceled') tradesRejected++;
      candidateDispositions.set(String(pr.symbol).toUpperCase(), `${rStatus || 'unknown'}:${result?.disposition || result?.settlement_status || 'execution_result'}`);
      executed.push({ symbol: pr.symbol, action: pr.action, qty: shares, price, ml_score: pr.ml_score, settlement: result });
    }

    const conservation = summarizeCandidateDispositions(candidates, candidateDispositions);
    if (!conservation.ok) {
      throw new Error(`SCAN_CANDIDATE_CONSERVATION_FAILED: missing ${conservation.missing.join(',')}`);
    }
    const executedIntentIds = new Set(executed.map((entry) => entry.settlement?.trade_intent_id).filter(Boolean));
    const [allFills, allSettlementEvents] = await Promise.all([
      sr.entities.Fill.filter({ user_id: user.id }),
      sr.entities.SettlementEvent.filter({ user_id: user.id }),
    ]);
    const scanFills = allFills.filter((fill) => executedIntentIds.has(fill.trade_intent_id));
    const scanSettlementEvents = allSettlementEvents.filter((event) => executedIntentIds.has(event.trade_intent_id));
    const settlementConservation = summarizeFillSettlement(scanFills, scanSettlementEvents);

    // NOTE: Self-learning weight evolution is now handled by the separate
    // Model Governance workflow (runModelGovernance). This cycle only executes
    // trades using the champion model's weights — it does NOT mutate the model.

    // Only count and notify about actually FILLED trades — not pending or
    // rejected attempts. (Fixes Rev.9 defects #15 + #16: persisted fill count
    // was inflated by counting non-rejected as filled; notifications mislabeled
    // attempts as executions.)
    const filledTrades = executed.filter((e) => e.settlement?.financially_complete === true);
    const unsettledFills = executed.filter((e) =>
      ['filled', 'partially_filled'].includes(e.settlement?.broker_status)
      && e.settlement?.fills_settlement_complete !== true
    );

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
      status: settlementConservation.ok ? 'completed' : 'failed',
      error: settlementConservation.ok ? null : `SETTLEMENT_CONSERVATION_FAILED: ${JSON.stringify(settlementConservation)}`,
      market_summary: p1.market_summary,
      market_regime: regime.market_regime,
      model_version: champion?.version || 'default',
      candidates_found: candidates.length,
      proposals_created: proposalsBeforeVeto,
      proposals_vetoed: Math.max(0, proposalsBeforeVeto - approved.length),
      trades_attempted: executed.length,
      trades_filled: filledTrades.length,
      trades_rejected: tradesRejected,
      candidates_accounted: conservation.accounted,
      candidate_dispositions: JSON.stringify(conservation.dispositions),
      ...settlementConservation,
    });

    // AUTO-PROMOTION CHECK — after trades settle, evaluate paper performance
    // and promote to live if the user's thresholds are met. Non-fatal — a
    // failure here never affects the scan result.
    try {
      await base44.functions.invoke('checkAutoPromotion', {});
    } catch (e) {
      try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'auto_promotion_check_failed', severity: 'warning', message: e.message }); } catch (auditError) { /* scan result remains authoritative */ }
    }

    return Response.json({
      ok: settlementConservation.ok,
      scan_run_id: runId,
      error: settlementConservation.ok ? null : `SETTLEMENT_CONSERVATION_FAILED: ${JSON.stringify(settlementConservation)}`,
      market_summary: p1.market_summary,
      risk_assessment: p2.risk_assessment,
      regime,
      champion_version: champion?.version || 'default',
      candidates_scanned: candidates.length,
      proposals_after_fit: proposals.length,
      proposals_after_committee: proposals.length,
      proposals_after_veto: approved.length,
      executed,
      candidate_conservation: conservation,
      settlement_conservation: settlementConservation,
      ml_weights: weights,
      unsettled_fills: unsettledFills.map((entry) => ({ symbol: entry.symbol, settlement_status: entry.settlement?.settlement_status, error: entry.settlement?.settlement_error })),
    }, { status: settlementConservation.ok ? 200 : 409 });
  } catch (error) {
    // Stop the periodic heartbeat
    if (heartbeatIntervalRef) clearInterval(heartbeatIntervalRef);
    // Finalize the scan run as failed. scanRunId and srRef are at the function
    // scope so this catch block can access them. (Fixes Rev.9 defect #1.)
    if (scanRunId && srRef) {
      try { await srRef.entities.ScanRun.update(scanRunId, { completed_at: new Date().toISOString(), status: 'failed', error: error.message }); }
      catch (e) {
        try { await srRef.entities.AuditEvent.create({ user_id: userIdRef, event_type: 'scan_finalization_failed', severity: 'error', entity_type: 'ScanRun', entity_id: scanRunId, message: `Failed to mark scan as failed: ${e.message}` }); } catch (ae) {}
      }
    }
    // Release the scan lock on failure — audit on failure instead of swallowing.
    // (Fixes Rev.13 #3.)
    if (lockOwnerToken && userIdRef && srRef) {
      try {
        const locks = await srRef.entities.ScanLock.filter({ user_id: userIdRef, owner_token: lockOwnerToken });
        for (const l of locks) {
          try { await srRef.entities.ScanLock.delete(l.id); }
          catch (e) {
            try { await srRef.entities.AuditEvent.create({ user_id: userIdRef, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Failed to delete scan lock ${l.id} on error: ${e.message}` }); } catch (ae) {}
          }
        }
      } catch (e) {
        try { await srRef.entities.AuditEvent.create({ user_id: userIdRef, event_type: 'lock_cleanup_failed', severity: 'warning', message: `Lock release query failed on error: ${e.message}` }); } catch (ae) {}
      }
    }
    return Response.json({ error: error.message }, { status: 500 });
  }
}
