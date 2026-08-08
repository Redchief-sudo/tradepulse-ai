// Canonical execution gateway — the single boundary every trading surface funnels through.
//
// State machine:
//   PROPOSED → RISK_APPROVED → SUBMITTED → ACCEPTED → (PARTIALLY_FILLED)* → FILLED → SETTLED
//   Failure:  REJECTED | CANCELED | EXPIRED | FAILED
//
// Key integrity guarantees:
// 1. STABLE IDEMPOTENCY: The caller provides an idempotency_key (or one is derived from
//    decision_id + signal_timestamp + symbol + side). The intent is persisted BEFORE
//    broker submission. A retried call resumes the SAME intent — never submits a second order.
// 2. PARTIAL FILL SAFETY: Partial fills are recorded incrementally. The intent stays
//    partially_filled until the broker reaches a terminal state (filled/canceled/done_for_day).
//    Each incremental fill gets its own unique fill_id and is idempotent.
// 3. IDEMPOTENT SETTLEMENT: Before inserting a Fill, we check if its fill_id already exists.
//    A retried settlement skips already-recorded fills — no duplicate accounting.
// 4. BROKER_PAPER MODE: Alpaca paper credentials submit to the Alpaca paper API.
//    internal_paper = simulated (no broker). broker_paper = Alpaca paper API. live = Alpaca live API.
// 5. FAIL-CLOSED: For broker_paper/live, a broker account lookup failure blocks new orders.
// 6. USER-SCOPING: All queries filter by user_id. All records set user_id = owner.
// 7. CREDENTIAL ISOLATION: Broker secrets are read from the BrokerCredential entity
//    (RLS-locked, service-role only) — never from the User object.

import { placeAlpacaOrder, getAlpacaOrder, getAlpacaAccount, getAlpacaLatestQuote } from './alpaca.ts';
import { evaluateRisk, riskLimitsForProfile, buildPortfolioSnapshot, checkDataFreshness, checkMaxDrawdown } from './riskEngine.ts';
import { fetchQuote } from './marketDataAdapter.ts';
import { isUsMarketOpen, usMarketSession } from './marketHours.ts';
import { AlpacaError } from './alpacaErrors.ts';
import { nowIso, genId } from './lotAccounting.ts';
import { executionSessionDecision } from './sessionState.ts';

const FILL_TIMEOUT_MS = 20000;
const POLL_INTERVAL_MS = 1000;

function isTerminalFailure(status) {
  return ['rejected', 'canceled', 'expired', 'replaced'].includes(status);
}
function isTerminal(status) {
  return ['filled', 'done_for_day', ...['rejected', 'canceled', 'expired', 'replaced']].includes(status);
}

export function executableQuoteMetrics(quote, side, nowMs = Date.now()) {
  const bid = Number(quote?.bid);
  const ask = Number(quote?.ask);
  const mid = (bid + ask) / 2;
  const observedMs = quote?.timestamp ? new Date(quote.timestamp).getTime() : NaN;
  const ageMs = Number.isFinite(observedMs) ? Math.max(0, nowMs - observedMs) : null;
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || ask < bid) return null;
  return {
    bid,
    ask,
    referencePrice: side === 'buy' ? ask : bid,
    estimatedSlippagePct: ((ask - bid) / 2 / mid) * 100,
    ageMs,
  };
}


// Fetch the authoritative executable quote for a symbol and persist it as a
// PriceSnapshot. The execution gateway does NOT trust the frontend/LLM price —
// it fetches the live quote itself and uses that same price for risk sizing,
// order metadata, and freshness validation. This eliminates the dependency on
// a separate snapshot workflow having already captured a newly-discovered
// symbol (the #1 blocker for first-time autonomous buys).
//
// The snapshot records the provider's observation timestamp (not the insertion
// time) so an after-hours close fetched at 9pm is not mistaken for fresh.
async function fetchAuthoritativeQuote(sr, userId, symbol, assetClass, finnhubKey) {
  const key = finnhubKey;
  const ac = assetClass || 'stocks';
  const q = await fetchQuote(symbol, ac, key);
  if (!q || q.error || !q.price || q.price <= 0) {
    return { ok: false, reason: 'NO_LIVE_QUOTE', price: null };
  }
  const providerTs = q.quote_timestamp
    ? new Date(q.quote_timestamp * 1000).toISOString()
    : null;
  if (!providerTs) return { ok: false, reason: 'QUOTE_TIMESTAMP_MISSING', price: null };
  const ageMs = Date.now() - new Date(providerTs).getTime();
  const maxAgeMs = ac === 'crypto' ? 60000 : 30000;
  if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs > maxAgeMs) {
    return { ok: false, reason: `STALE_LIVE_QUOTE age_ms=${Number.isFinite(ageMs) ? ageMs : 'invalid'}`, price: null };
  }
  const session = usMarketSession(new Date(providerTs));
  try {
    await sr.entities.PriceSnapshot.create({
      user_id: userId,
      symbol: String(symbol).toUpperCase(),
      price: q.price,
      timestamp: nowIso(),
      provider_timestamp: providerTs,
      market_session: session,
      is_market_open: session === 'regular',
      source: ac === 'crypto' ? 'coinbase' : 'finnhub',
    });
  } catch (e) { /* non-fatal — snapshot is for freshness checks, not execution */ }
  return { ok: true, price: q.price, provider_timestamp: providerTs, market_session: session };
}

// Derive a stable idempotency key from the input. The caller may provide one directly,
// or we derive it from decision_id + signal_timestamp + symbol + side. This ensures
// that a retried call (e.g. browser retry, workflow re-fire) resumes the same intent
// rather than submitting a second broker order.
function deriveIdempotencyKey(input) {
  if (input.idempotency_key) return String(input.idempotency_key);
  const decisionId = input.decision_id || '';
  const signalTs = input.signal_timestamp || '';
  const symbol = String(input.symbol || '').toUpperCase();
  const side = input.action || input.side || '';
  const source = input.source || input.strategy_id || '';
  // If we have enough signal identity, derive a stable key.
  if (decisionId || signalTs) {
    return `ik-${source}-${decisionId || signalTs}-${symbol}-${side}`;
  }
  // Fallback: no stable identity — return null (a new intent will be created).
  return null;
}

// Read broker credentials from the BrokerCredential entity (RLS-locked, service-role only).
// The User object never carries the secret — it only carries broker/mode/connected flags.
async function loadBrokerCredentials(sr, userId) {
  const creds = await sr.entities.BrokerCredential.filter({
    user_id: userId,
    status: 'active',
  });
  if (!creds || creds.length === 0) return null;
  return creds[0];
}

// Resolve the execution environment from the user profile + credential state.
// internal_paper  → simulated fill, no broker submission
// broker_paper    → Alpaca paper API (real paper orders, real paper fills)
// live            → Alpaca live API (real money)
// shadow_live     → simulated fill at live reference price (no broker submission)
function resolveExecutionMode(user, input, brokerCred) {
  if (input.execution_mode && ['internal_paper', 'broker_paper', 'paper', 'shadow_live', 'live'].includes(input.execution_mode)) {
    return input.execution_mode === 'paper' ? 'internal_paper' : input.execution_mode;
  }
  if (brokerCred && brokerCred.mode === 'live') return 'live';
  if (brokerCred && brokerCred.mode === 'paper') return 'broker_paper';
  return 'internal_paper';
}

// Record a single fill increment idempotently using CREATE-THEN-CHECK.
// Without DB-level unique constraints, check-then-act has a race window where
// two workers both see "no existing fill" and both create. Create-then-check
// narrows the window: both create, both check, the earliest record wins and
// the duplicate is deleted. This is the best available mitigation on a BaaS
// platform without unique constraints.
export async function recordFill(sr, params) {
  const created = await sr.entities.Fill.create(params);
  const all = await sr.entities.Fill.filter({ user_id: params.user_id, fill_id: params.fill_id });
  if (all.length > 1) {
    // Duplicate detected — keep the earliest, delete the rest.
    const sorted = all.sort((a, b) => {
      const da = new Date(a.created_date).getTime();
      const db = new Date(b.created_date).getTime();
      if (da !== db) return da - db;
      return a.id < b.id ? -1 : 1; // deterministic tiebreaker
    });
    if (sorted[0].id !== created.id) {
      // We lost the race — delete our duplicate.
      await sr.entities.Fill.delete(created.id);
      return false;
    }
    // We won — clean up any other duplicates.
    for (let i = 1; i < sorted.length; i++) {
      await sr.entities.Fill.delete(sorted[i].id);
    }
  }
  return true;
}

// Structured audit logging — records every critical execution event to the
// AuditEvent entity for observability, compliance, and incident investigation.
async function audit(sr, userId, eventType, severity, details) {
  try {
    await sr.entities.AuditEvent.create({
      user_id: userId,
      event_type: eventType,
      severity,
      correlation_id: details.correlation_id || null,
      entity_type: details.entity_type || null,
      entity_id: details.entity_id || null,
      message: details.message || '',
      details: JSON.stringify(details),
      broker_api_latency_ms: details.broker_api_latency_ms || null,
    });
  } catch (e) { /* non-fatal — audit must never block execution */ }
}

// executeIntent — the canonical gateway.
export async function executeIntent(base44, user, input) {
  const sr = base44.asServiceRole;
  const userId = user.id;
  const symbol = String(input.symbol || '').toUpperCase();
  const side = input.action || input.side;
  const requestedQty = Number(input.qty || input.requested_quantity);
  if (!symbol || (side !== 'buy' && side !== 'sell') || !requestedQty || requestedQty <= 0) {
    return { status: 'invalid', error: 'symbol, side, and positive qty are required' };
  }
  let refPrice = Number(input.price) || 0;
  if (!refPrice || refPrice <= 0) {
    return { status: 'invalid', error: 'a reference price is required' };
  }

  // Reload server-authoritative session state at the execution boundary. The
  // caller's User snapshot may predate a settlement/reconciliation block.
  let authoritativeUser;
  try {
    authoritativeUser = await sr.entities.User.get(userId);
  } catch (error) {
    return { status: 'rejected', error: `SESSION_STATE_UNAVAILABLE: ${error.message}`, symbol, side, requestedQty };
  }
  const sessionDecision = executionSessionDecision(authoritativeUser, side);
  if (!sessionDecision.allowed) {
    return { status: 'rejected', reasons: [sessionDecision.reason], symbol, side, requestedQty };
  }
  user = authoritativeUser;

  // Load broker credentials from the secure BrokerCredential entity.
  const brokerCred = await loadBrokerCredentials(sr, userId);
  const executionMode = resolveExecutionMode(user, input, brokerCred);
  const strategyId = input.source || input.strategy_id || 'manual';
  const signalTs = input.signal_timestamp || nowIso();

  // 1. STABLE IDEMPOTENCY — check for an existing intent by idempotency key.
  const idempotencyKey = deriveIdempotencyKey(input);
  let intentRecord = null;
  if (idempotencyKey) {
    const existing = await sr.entities.TradeIntent.filter({ user_id: userId, idempotency_key: idempotencyKey });
    if (existing && existing.length > 0) {
      intentRecord = existing[0];
      // If already settled/filled, return the existing result (idempotent retry).
      if (['settled', 'filled', 'rejected', 'canceled', 'expired', 'failed'].includes(intentRecord.status)) {
        return {
          status: intentRecord.status === 'settled' || intentRecord.status === 'filled' ? 'filled' : 'rejected',
          intentId: intentRecord.id,
          trade_intent_id: intentRecord.trade_intent_id,
          brokerOrderId: intentRecord.broker_order_id,
          clientOrderId: intentRecord.client_order_id,
          filled_qty: intentRecord.filled_quantity,
          filled_avg_price: intentRecord.filled_avg_price,
          execution_mode: intentRecord.execution_mode,
          symbol, action: side,
          resumed: true,
        };
      }
      // If in-progress (submitted/accepted/partially_filled), resume polling.
      if (['submitted', 'accepted', 'partially_filled', 'risk_approved'].includes(intentRecord.status)) {
        return await resumeBrokerOrder(base44, sr, userId, intentRecord, input, brokerCred);
      }
      // If proposed, continue the flow from risk evaluation.
    }
  }

  const tradeIntentId = intentRecord?.trade_intent_id || genId('tpi');
  const clientOrderId = intentRecord?.client_order_id || genId('tp');

  // 1a. PROPOSED — persist the canonical intent (or update existing).
  const intentData = {
    user_id: userId,
    trade_intent_id: tradeIntentId,
    idempotency_key: idempotencyKey,
    strategy_id: strategyId,
    decision_id: input.decision_id || intentRecord?.decision_id || null,
    portfolio_id: input.portfolio_id || null,
    asset_class: input.asset_class || 'stocks',
    symbol,
    native_asset_id: input.native_asset_id || symbol,
    broker: brokerCred ? brokerCred.broker : 'internal',
    side,
    order_type: input.order_type || 'market',
    requested_quantity: requestedQty,
    requested_notional: requestedQty * refPrice,
    limit_price: input.limit_price || (input.order_type === 'limit' ? refPrice : null),
    stop_price: input.stop_price || null,
    time_in_force: input.time_in_force || 'day',
    execution_mode: executionMode,
    status: 'proposed',
    signal_timestamp: signalTs,
    decision_timestamp: nowIso(),
    client_order_id: clientOrderId,
    company_name: input.company_name || symbol,
    sector: input.sector || '',
    confidence: input.confidence,
    target_price: input.target_price,
    stop_loss: input.stop_loss,
    reasoning: input.reasoning,
    ml_score: input.ml_score,
    technical_score: input.technical_score,
    momentum_score: input.momentum_score,
    risk_score: input.risk_score,
    regime: input.regime || null,
  };

  if (intentRecord) {
    await sr.entities.TradeIntent.update(intentRecord.id, intentData);
  } else {
    intentRecord = await sr.entities.TradeIntent.create(intentData);
  }

  // 1b. MARKET HOURS GATE for equities in every execution mode. The backend independently
  // verifies the US regular session is open before submitting a broker order.
  // Cron timing alone is insufficient — the gateway is the authoritative gate.
  // A day order submitted after hours can queue and fill at a gapped next open.
  // Checked before the quote fetch so we don't burn a provider call when the
  // session is closed.
  if (String(input.asset_class || 'stocks').toLowerCase() !== 'crypto') {
    if (!isUsMarketOpen()) {
      const session = usMarketSession();
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'rejected',
        rejection_reason: `MARKET_CLOSED (${session})`,
      });
      await audit(sr, userId, 'stale_data_block', 'warning', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Rejected ${side} ${requestedQty} ${symbol}: market closed (${session})`, details: { symbol, side, session } });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: [`MARKET_CLOSED (${session})`], symbol, side, requestedQty };
    }
  }

  // 1b.5. AUTHORITATIVE QUOTE. Every mode uses a provider-backed price; internal
  // paper must never turn a frontend/LLM estimate into a simulated fill. Broker
  // modes additionally require Alpaca top-of-book data for spread/slippage gates.
  // The gateway uses that quote—not the frontend/LLM price—for risk sizing and
  // order metadata. This is the single source of
  // truth for the execution reference price. It also persists a fresh
  // PriceSnapshot (with the provider's observation timestamp) so subsequent
  // freshness checks and outcome labeling have an authoritative record, and
  // so newly-discovered symbols are never rejected for a missing snapshot.
  let executableQuote = null;
  let brokerExecutableSymbol = symbol;
  if (executionMode === 'live' || executionMode === 'broker_paper') {
    try {
      const rawQuote = await getAlpacaLatestQuote(
        { apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret },
        symbol,
        input.asset_class || 'stocks'
      );
      executableQuote = executableQuoteMetrics(rawQuote, side);
      brokerExecutableSymbol = rawQuote.symbol;
      const maxQuoteAgeMs = String(input.asset_class || 'stocks').toLowerCase() === 'crypto' ? 60000 : 30000;
      if (!executableQuote || executableQuote.ageMs == null || executableQuote.ageMs > maxQuoteAgeMs) {
        throw new Error(`STALE_EXECUTABLE_QUOTE: age_ms=${executableQuote?.ageMs ?? 'unknown'}`);
      }
      refPrice = executableQuote.referencePrice;
      try {
        const isCrypto = String(input.asset_class || 'stocks').toLowerCase() === 'crypto';
        await sr.entities.PriceSnapshot.create({
          user_id: userId,
          symbol,
          price: refPrice,
          timestamp: nowIso(),
          provider_timestamp: rawQuote.timestamp,
          source: rawQuote.source,
          ...(isCrypto ? {} : {
            market_session: usMarketSession(new Date(rawQuote.timestamp)),
            is_market_open: isUsMarketOpen(),
          }),
        });
      } catch (e) { /* non-fatal — execution still uses the verified quote */ }
    } catch (e) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `NO_EXECUTABLE_QUOTE: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: `No executable broker quote: ${e.message}`, symbol, side, requestedQty };
    }
  } else {
    const quote = await fetchAuthoritativeQuote(sr, userId, symbol, input.asset_class, input.finnhub_key);
    if (!quote.ok) {
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'rejected',
        rejection_reason: `${quote.reason}: no fresh executable quote for ${symbol}`,
      });
      await audit(sr, userId, 'stale_data_block', 'warning', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Rejected ${side} ${requestedQty} ${symbol}: ${quote.reason}`, details: { symbol, side, reason: quote.reason } });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: [`${quote.reason}: ${symbol}`], symbol, side, requestedQty };
    }
    // Override the reference price with the authoritative quote. All downstream
    // sizing, risk caps, and order metadata use this price.
    refPrice = quote.price;
  }
  await sr.entities.TradeIntent.update(intentRecord.id, {
    requested_notional: requestedQty * refPrice,
    limit_price: (intentRecord.order_type === 'limit' || input.order_type === 'limit') ? refPrice : null,
  });

  // 1c. Fetch real account equity for risk sizing (broker_paper/live only).
  // FAIL-CLOSED: if account is unreachable or has no equity, reject before risk
  // evaluation — no order is submitted. The equity is passed to buildPortfolioSnapshot
  // so position/sector caps are computed against the real capital base, not stale
  // holding cache drift.
  let accountEquity = null;
  let brokerAccount = null;
  if (executionMode === 'live' || executionMode === 'broker_paper') {
    try {
      brokerAccount = await getAlpacaAccount({ apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret, mode: brokerCred.mode });
      if (!brokerAccount || Number(brokerAccount.equity) <= 0) {
        await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: 'BROKER_ACCOUNT_INVALID: no equity' });
        return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: 'Broker account invalid', symbol, side, requestedQty };
      }
      accountEquity = Number(brokerAccount.equity);
    } catch (e) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `BROKER_UNREACHABLE: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: `Broker unreachable: ${e.message}`, symbol, side, requestedQty };
    }
  }

  // 2. RISK evaluation — deterministic, veto authority. DENIED ⇒ zero order, zero mutation.
  // Uses real broker account equity when available (broker_paper/live), falls back to
  // holdings-based equity for internal_paper mode. Passes broker prev-close equity so
  // the daily-loss check uses broker equity decline, not app-realized P&L.
  // (Fixes Rev.15 #9: scan and execution had inconsistent daily-loss definitions.)
  let brokerPrevCloseEquity = null;
  if (executionMode === 'live' || executionMode === 'broker_paper') {
    brokerPrevCloseEquity = Number(brokerAccount?.last_equity) || null;
    if (side === 'buy' && !brokerPrevCloseEquity) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: 'BROKER_DAILY_LOSS_BASELINE_UNAVAILABLE' });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: 'BROKER_DAILY_LOSS_BASELINE_UNAVAILABLE', symbol, side, requestedQty };
    }
  }
  const snapshot = await buildPortfolioSnapshot(sr, userId, accountEquity, brokerPrevCloseEquity);
  const limits = riskLimitsForProfile(user.trade_profile || 'balanced');

  // Max drawdown check — compute before evaluateRisk so it can be enforced
  // in the canonical risk engine, not just the scan cycle.
  // (Fixes Rev.14 #13: other trade surfaces bypassed the scan-level drawdown guard.)
  let maxDrawdownBreached = false;
  if (side === 'buy' && limits.max_drawdown_pct && snapshot.totalEquity > 0) {
    try {
      const ddCheck = await checkMaxDrawdown(sr, userId, snapshot.totalEquity, limits);
      if (ddCheck.breached) {
        maxDrawdownBreached = true;
        await audit(sr, userId, 'max_drawdown_breach', 'critical', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Max drawdown limit reached: ${ddCheck.drawdown_pct}% (limit ${ddCheck.limit}%, peak $${ddCheck.peak_equity.toFixed(0)})` });
      }
    } catch (e) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `DRAWDOWN_STATE_UNAVAILABLE: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: `DRAWDOWN_STATE_UNAVAILABLE: ${e.message}`, symbol, side, requestedQty };
    }
  }

  // Internal paper / shadow mode have no real market data — skip spread and
  // slippage checks with a documented exemption. Broker mode must pass bid/ask
  // and slippage estimates or be rejected. (Fixes Rev.14 #12.)
  const skipMarketDataChecks = executionMode === 'internal_paper' || executionMode === 'shadow_live';

  const risk = evaluateRisk(
    { symbol, side, requested_quantity: requestedQty, limit_price: refPrice, price: refPrice, sector: input.sector, confidence: input.confidence, stop_loss: input.stop_loss },
    snapshot,
    limits,
    {
      killSwitch: !!user.kill_switch_reset_required || user.trading_session_state === 'risk_stopped',
      maxDrawdownBreached,
      skipMarketDataChecks,
      bid: executableQuote?.bid,
      ask: executableQuote?.ask,
      estimated_slippage_pct: executableQuote?.estimatedSlippagePct,
    }
  );

  if (!risk.approved) {
    await sr.entities.TradeIntent.update(intentRecord.id, {
      status: 'rejected',
      rejection_reason: risk.reasons.join('; '),
      risk_snapshot: JSON.stringify({ reasons: risk.reasons, limits, snapshot: { totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, tradesToday: snapshot.tradesToday, dailyPnlPct: snapshot.dailyPnlPct } }),
    });
    await audit(sr, userId, 'order_rejected', 'warning', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Risk rejected ${side} ${requestedQty} ${symbol}: ${risk.reasons.join('; ')}`, details: { symbol, side, qty: requestedQty, reasons: risk.reasons } });
    return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: risk.reasons, symbol, side, requestedQty };
  }

  const approvedQty = risk.approvedQuantity;
  const estimatedNotional = approvedQty * refPrice;
  await sr.entities.TradeIntent.update(intentRecord.id, {
    status: 'risk_approved',
    requested_quantity: approvedQty,
    reserved_cash: side === 'buy' ? estimatedNotional + (input.commission || 0) + (input.fees || 0) : 0,
    consumed_cash: 0,
    released_cash: 0,
    reservation_status: side === 'buy' ? 'reserved' : 'none',
    risk_snapshot: JSON.stringify({ reasons: risk.reasons, approvedQuantity: approvedQty, limits, snapshot: { totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, tradesToday: snapshot.tradesToday, dailyPnlPct: snapshot.dailyPnlPct } }),
    portfolio_snapshot: JSON.stringify({ totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, sectorMap: snapshot.sectorMap }),
  });

  // External quote/account/risk calls may take long enough for an operator or
  // reconciliation worker to stop trading. Revalidate immediately before the
  // irreversible fill/submission boundary.
  let preSubmissionUser;
  try {
    preSubmissionUser = await sr.entities.User.get(userId);
  } catch (error) {
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `SESSION_STATE_UNAVAILABLE: ${error.message}` });
    return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: `SESSION_STATE_UNAVAILABLE: ${error.message}`, symbol, side, requestedQty };
  }
  const preSubmissionDecision = executionSessionDecision(preSubmissionUser, side);
  if (!preSubmissionDecision.allowed) {
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: preSubmissionDecision.reason });
    return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: [preSubmissionDecision.reason], symbol, side, requestedQty };
  }

  // 3. Execution by environment.
  if (executionMode === 'internal_paper' || executionMode === 'shadow_live') {
    // Simulated fill at the reference price. No broker submission.
    const filledQty = approvedQty;
    const filledPrice = refPrice;
    const venue = executionMode === 'shadow_live' ? 'shadow' : 'paper';
    const fillId = genId('fill');

    // Record the Fill (raw execution ledger — idempotent by fill_id)
    await recordFill(sr, {
      user_id: userId,
      fill_id: fillId,
      broker_order_id: null,
      client_order_id: clientOrderId,
      trade_intent_id: tradeIntentId,
      symbol,
      asset_id: input.native_asset_id || symbol,
      side,
      filled_quantity: filledQty,
      filled_price: filledPrice,
      notional: filledQty * filledPrice,
      commission: input.commission || 0,
      fees: input.fees || 0,
      slippage: 0,
      timestamp: nowIso(),
      venue,
      strategy_id: strategyId,
      decision_id: input.decision_id || null,
      regime: intentRecord.regime || input.regime || null,
      portfolio_id: intentRecord.portfolio_id || input.portfolio_id || null,
      execution_mode: executionMode,
      asset_class: input.asset_class || 'stocks',
      arrival_price: Number(input.arrival_price || input.price) || null,
      decision_price: Number(input.decision_price || input.price) || null,
      submission_price: Number(input.price) || null,
      first_fill_price: filledPrice,
      vwap_fill_price: filledPrice,
      implementation_shortfall: Number(input.price) && filledPrice ? Number(input.price) - filledPrice : null,
      fill_latency_ms: null,
    });

    // Create a SettlementEvent — the settlement processor handles all projection
    // (lots, cash, holding, trade, P&L). This is the single-writer boundary:
    // no execution request directly mutates CashEntry/PositionLot/Holding.
    await sr.entities.SettlementEvent.create({
      user_id: userId,
      portfolio_id: intentRecord.portfolio_id || input.portfolio_id || null,
      event_id: fillId,
      trade_intent_id: tradeIntentId,
      broker_order_id: null,
      broker_fill_id: null,
      client_order_id: clientOrderId,
      symbol, side,
      quantity: filledQty, price: filledPrice,
      notional: filledQty * filledPrice,
      commission: input.commission || 0, fees: input.fees || 0,
      occurred_at: nowIso(),
      status: 'pending',
      execution_mode: executionMode,
      asset_class: input.asset_class || 'stocks',
      company_name: input.company_name || symbol,
      sector: input.sector || '',
      strategy_id: strategyId,
      decision_id: input.decision_id || null,
      regime: intentRecord.regime || input.regime || null,
      venue,
      arrival_price: Number(input.arrival_price || input.price) || null,
      decision_price: Number(input.decision_price || input.price) || null,
      submission_price: Number(input.price) || null,
      first_fill_price: filledPrice,
      vwap_fill_price: filledPrice,
      implementation_shortfall: Number(input.price) && filledPrice ? Number(input.price) - filledPrice : null,
    });

    await sr.entities.TradeIntent.update(intentRecord.id, {
      status: 'filled',
      filled_quantity: filledQty,
      filled_avg_price: filledPrice,
    });

    // Invoke the settlement processor to project lots, cash, holding, trade.
    // For internal_paper this is synchronous (immediate feedback). The processor
    // is idempotent — a retry or scheduled invocation won't double-project.
    try {
      await base44.functions.invoke('processSettlementQueue', {});
    } catch (e) { /* non-fatal — scheduled processor will retry */ }

    return {
      status: 'paper_filled',
      intentId: intentRecord.id,
      trade_intent_id: tradeIntentId,
      clientOrderId,
      filled_qty: filledQty,
      filled_avg_price: filledPrice,
      total_value: filledQty * filledPrice,
      execution_mode: executionMode,
      symbol,
      action: side,
    };
  }

  // broker_paper or live — submit to Alpaca.
  if (!brokerCred) {
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: 'NO_BROKER_CREDENTIALS' });
    return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: 'No active broker credentials', symbol, side, requestedQty: approvedQty };
  }

  const creds = { apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret, mode: brokerCred.mode };
  const alpacaMode = executionMode === 'live' ? 'live' : 'paper';

  // 3b. Submit the order (only if not already submitted — resume case).
  // Account was already verified and equity fetched in step 1c above.
  let brokerOrderId = intentRecord.broker_order_id;
  if (!brokerOrderId) {
    try {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'submitted', broker: brokerCred.broker, submitted_at: nowIso() });
      const placed = await placeAlpacaOrder({
        ...creds, mode: alpacaMode, symbol: brokerExecutableSymbol, qty: approvedQty, side, client_order_id: clientOrderId,
        order_type: intentRecord.order_type || 'market',
        limit_price: intentRecord.limit_price,
        stop_price: intentRecord.stop_price,
        time_in_force: intentRecord.time_in_force || 'day',
      });
      brokerOrderId = placed.id;
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'accepted', broker_order_id: brokerOrderId, client_order_id: clientOrderId, accepted_at: nowIso(), last_broker_update_at: nowIso() });
      await audit(sr, userId, 'order_submitted', 'info', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Order ${brokerOrderId} accepted: ${side} ${approvedQty} ${symbol}`, details: { symbol, side, qty: approvedQty, broker_order_id: brokerOrderId } });
    } catch (e) {
      const errorDetail = e instanceof AlpacaError ? e.toAuditDetail() : { message: e.message };
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `BROKER_SUBMIT_ERROR: ${e.message}` });
      await audit(sr, userId, 'order_rejected', 'error', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Broker submit error: ${e.message}`, details: { ...errorDetail, symbol, side, qty: approvedQty } });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: e.message, symbol, side, requestedQty: approvedQty };
    }
  }

  // 3c. Poll until terminal state. Record each incremental fill idempotently.
  return await pollAndSettle(base44, sr, userId, intentRecord, creds, alpacaMode, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId);
}

// Poll a broker order until terminal state, recording incremental fills.
// A partial fill records its increment but keeps the intent partially_filled.
// Only when the order reaches filled/canceled/done_for_day/rejected do we settle.
async function pollAndSettle(base44, sr, userId, intentRecord, creds, alpacaMode, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId) {
  const symbol = intentRecord.symbol;
  const side = intentRecord.side;
  const executionMode = intentRecord.execution_mode;
  let lastFilledQty = intentRecord.filled_quantity || 0;
  let lastFilledPrice = intentRecord.filled_avg_price || 0;
  const venue = executionMode === 'live' ? 'alpaca' : 'alpaca_paper';

  const start = Date.now();
  while (Date.now() - start < FILL_TIMEOUT_MS) {
    let order;
    try {
      order = await getAlpacaOrder(creds, brokerOrderId);
      // Update last_broker_update_at so the health check can use it for
      // pending-order age calculations. (Fixes Rev.12 #22.)
      try { await sr.entities.TradeIntent.update(intentRecord.id, { last_broker_update_at: nowIso() }); }
      catch (e) {
        // Audit on failure — a stale last_broker_update_at can cause the health
        // check to misclassify order age. (Fixes Rev.13 #14.)
        await audit(sr, userId, 'broker_timestamp_update_failed', 'warning', { correlation_id: tradeIntentId, entity_type: 'TradeIntent', entity_id: intentRecord.id, message: `Failed to update last_broker_update_at: ${e.message}` });
      }
    } catch (e) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      continue;
    }

    const status = order.status;
    const cumulativeFilled = Number(order.filled_qty) || 0;
    const cumulativeAvgPrice = Number(order.filled_avg_price) || 0;

    // Record incremental fill if new shares have been filled.
    if (cumulativeFilled > lastFilledQty) {
      const incrementalQty = cumulativeFilled - lastFilledQty;
      // Derive the INCREMENTAL fill price from cumulative quantities and VWAPs.
      // Alpaca's filled_avg_price is the VWAP of ALL fills so far. Using it directly
      // for each increment corrupts the fill ledger (second fill gets cumulative avg,
      // not its own price). The correct incremental price is:
      //   incremental_notional = new_cum_qty × new_avg − prev_cum_qty × prev_avg
      //   incremental_price = incremental_notional / incremental_qty
      const prevNotional = lastFilledQty * (lastFilledPrice || 0);
      const currNotional = cumulativeFilled * cumulativeAvgPrice;
      const incrementalNotional = currNotional - prevNotional;
      const incrementalPrice = incrementalQty > 0 && incrementalNotional > 0
        ? incrementalNotional / incrementalQty
        : (cumulativeAvgPrice > 0 ? cumulativeAvgPrice : Number(input.price));

      const fillId = `${brokerOrderId}:fill:${cumulativeFilled}`;
      const recorded = await recordFill(sr, {
        user_id: userId,
        fill_id: fillId,
        broker_order_id: brokerOrderId,
        client_order_id: clientOrderId,
        trade_intent_id: tradeIntentId,
        symbol,
        asset_id: input.native_asset_id || symbol,
        side,
        filled_quantity: incrementalQty,
        filled_price: incrementalPrice,
        notional: incrementalQty * incrementalPrice,
        commission: input.commission || 0,
        fees: input.fees || 0,
        slippage: 0,
        timestamp: nowIso(),
        venue,
        strategy_id: strategyId,
        decision_id: input.decision_id || null,
        portfolio_id: intentRecord.portfolio_id || input.portfolio_id || null,
        execution_mode: executionMode,
        asset_class: input.asset_class || 'stocks',
        arrival_price: Number(input.arrival_price || input.price) || null,
        decision_price: Number(input.decision_price || input.price) || null,
        submission_price: Number(input.price) || null,
        first_fill_price: lastFilledQty === 0 ? incrementalPrice : null,
        vwap_fill_price: cumulativeAvgPrice > 0 ? cumulativeAvgPrice : null,
        implementation_shortfall: Number(input.price) && incrementalPrice ? Number(input.price) - incrementalPrice : null,
        fill_latency_ms: null,
      });

      if (recorded) {
        // Create a SettlementEvent — the settlement processor handles all projection
        // (lots, cash, holding, trade, P&L). This is the single-writer boundary.
        await sr.entities.SettlementEvent.create({
          user_id: userId,
          portfolio_id: intentRecord.portfolio_id || input.portfolio_id || null,
          event_id: fillId,
          trade_intent_id: tradeIntentId,
          broker_order_id: brokerOrderId,
          broker_fill_id: fillId,
          client_order_id: clientOrderId,
          symbol, side,
          quantity: incrementalQty, price: incrementalPrice,
          notional: incrementalQty * incrementalPrice,
          commission: input.commission || 0, fees: input.fees || 0,
          occurred_at: nowIso(),
          status: 'pending',
          execution_mode: executionMode,
          asset_class: input.asset_class || 'stocks',
          company_name: input.company_name || symbol,
          sector: input.sector || '',
          strategy_id: strategyId,
          decision_id: input.decision_id || null,
          regime: intentRecord.regime || input.regime || null,
          venue,
          arrival_price: Number(input.arrival_price || input.price) || null,
          decision_price: Number(input.decision_price || input.price) || null,
          submission_price: Number(input.price) || null,
          first_fill_price: lastFilledQty === 0 ? incrementalPrice : null,
          vwap_fill_price: cumulativeAvgPrice > 0 ? cumulativeAvgPrice : null,
          implementation_shortfall: Number(input.price) && incrementalPrice ? Number(input.price) - incrementalPrice : null,
          fill_latency_ms: null,
        });
      }

      lastFilledQty = cumulativeFilled;
      lastFilledPrice = cumulativeAvgPrice;
    }

    // Check for terminal state.
    if (status === 'filled' || status === 'done_for_day') {
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'filled',
        filled_quantity: cumulativeFilled,
        filled_avg_price: cumulativeAvgPrice,
        broker_terminal_status: status,
        unfilled_quantity: Math.max(0, intentRecord.requested_quantity - cumulativeFilled),
      });
      // Invoke the settlement processor to project all pending events for this order.
      try { await base44.functions.invoke('processSettlementQueue', {}); }
      catch (e) { /* non-fatal — scheduled processor will retry */ }
      return {
        status: executionMode === 'live' ? 'filled' : 'paper_filled',
        intentId: intentRecord.id, trade_intent_id: tradeIntentId,
        brokerOrderId, clientOrderId,
        filled_qty: cumulativeFilled, filled_avg_price: cumulativeAvgPrice,
        total_value: cumulativeFilled * cumulativeAvgPrice,
        execution_mode: executionMode, symbol, action: side,
      };
    }

    if (isTerminalFailure(status)) {
      if (cumulativeFilled > 0) {
        await sr.entities.TradeIntent.update(intentRecord.id, {
          status: 'partially_filled',
          filled_quantity: cumulativeFilled,
          filled_avg_price: cumulativeAvgPrice,
          broker_terminal_status: status,
          unfilled_quantity: Math.max(0, intentRecord.requested_quantity - cumulativeFilled),
          released_cash: Math.max(0, (intentRecord.reserved_cash || 0) - (intentRecord.consumed_cash || 0)),
          reservation_status: 'released',
        });
        try { await base44.functions.invoke('processSettlementQueue', {}); }
        catch (e) { /* non-fatal — scheduled processor will retry */ }
        return {
          status: 'partially_filled',
          intentId: intentRecord.id, trade_intent_id: tradeIntentId,
          brokerOrderId, clientOrderId,
          filled_qty: cumulativeFilled, filled_avg_price: cumulativeAvgPrice,
          execution_mode: executionMode, symbol, action: side,
        };
      }
      // No fill at all — reject and release the full reservation.
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: status === 'rejected' ? 'rejected' : 'canceled',
        rejection_reason: `BROKER_${status.toUpperCase()}`,
        broker_terminal_status: status,
        unfilled_quantity: intentRecord.requested_quantity,
        released_cash: intentRecord.reserved_cash || 0,
        reserved_cash: 0,
        reservation_status: 'released',
      });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, orderStatus: status, brokerOrderId, symbol, side, requestedQty: intentRecord.requested_quantity };
    }

    if (status === 'partially_filled') {
      // Update intent to partially_filled but keep polling.
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'partially_filled',
        filled_quantity: cumulativeFilled,
        filled_avg_price: cumulativeAvgPrice,
      });
    }

    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }

  // Timeout — order is still pending. Return pending status; a reconciliation
  // workflow will pick up the final state later.
  await sr.entities.TradeIntent.update(intentRecord.id, {
    status: lastFilledQty > 0 ? 'partially_filled' : 'accepted',
    filled_quantity: lastFilledQty,
    filled_avg_price: lastFilledPrice,
  });
  return {
    status: 'pending',
    intentId: intentRecord.id,
    trade_intent_id: tradeIntentId,
    brokerOrderId,
    symbol, side,
    filled_qty: lastFilledQty,
    filled_avg_price: lastFilledPrice,
  };
}

// Resume an in-progress broker order (idempotent retry path).
async function resumeBrokerOrder(base44, sr, userId, intentRecord, input, brokerCred) {
  if (!intentRecord.broker_order_id) {
    // Not yet submitted — re-enter the main flow from risk evaluation.
    return null; // caller will continue
  }
  const creds = { apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret, mode: brokerCred.mode };
  const alpacaMode = intentRecord.execution_mode === 'live' ? 'live' : 'paper';
  return await pollAndSettle(
    base44, sr, userId, intentRecord, creds, alpacaMode, input,
    intentRecord.broker_order_id, intentRecord.client_order_id, intentRecord.trade_intent_id, intentRecord.strategy_id
  );
}

// Backward-compatible wrapper.
export async function settleTrade(base44, user, intent) {
  return executeIntent(base44, user, intent);
}
