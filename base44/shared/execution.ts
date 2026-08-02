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

import { placeAlpacaOrder, getAlpacaOrder, getAlpacaAccount } from './alpaca.ts';
import { evaluateRisk, riskLimitsForProfile, buildPortfolioSnapshot, checkDataFreshness } from './riskEngine.ts';

const FILL_TIMEOUT_MS = 20000;
const POLL_INTERVAL_MS = 1000;

function isTerminalFailure(status) {
  return ['rejected', 'canceled', 'expired', 'replaced'].includes(status);
}
function isTerminal(status) {
  return ['filled', 'done_for_day', ...['rejected', 'canceled', 'expired', 'replaced']].includes(status);
}
function nowIso() { return new Date().toISOString(); }
function genId(prefix) { return `${prefix}-${crypto.randomUUID()}`; }

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

// Check if a fill_id already exists — idempotent fill insertion.
async function fillExists(sr, fillId) {
  const existing = await sr.entities.Fill.filter({ fill_id: fillId });
  return existing && existing.length > 0;
}

// Record a single fill increment idempotently. If the fill_id already exists, skip.
// Returns true if a new fill was recorded, false if it was already present.
async function recordFill(sr, params) {
  if (await fillExists(sr, params.fill_id)) return false;
  await sr.entities.Fill.create(params);
  return true;
}

// Project a holding from a fill — the Holding is a cache of the Fill ledger.
async function projectHolding(sr, userId, symbol, side, filledQty, filledPrice, input) {
  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const existing = holdings.find((h) => String(h.symbol).toUpperCase() === symbol);
  const totalValue = filledQty * filledPrice;

  if (side === 'buy') {
    if (existing) {
      const ts = existing.shares + filledQty;
      const tc = existing.shares * existing.avg_price + totalValue;
      await sr.entities.Holding.update(existing.id, {
        shares: ts,
        avg_price: tc / ts,
        current_price: filledPrice,
        stop_loss: input.stop_loss ?? existing.stop_loss,
        target_price: input.target_price ?? existing.target_price,
      });
    } else {
      await sr.entities.Holding.create({
        user_id: userId,
        symbol,
        company_name: input.company_name || symbol,
        shares: filledQty,
        avg_price: filledPrice,
        current_price: filledPrice,
        sector: input.sector || '',
        day_change_percent: 0,
        stop_loss: input.stop_loss,
        target_price: input.target_price,
        asset_class: input.asset_class || 'stocks',
      });
    }
  } else {
    if (existing) {
      const newShares = existing.shares - filledQty;
      if (newShares <= 0.0001) await sr.entities.Holding.delete(existing.id);
      else await sr.entities.Holding.update(existing.id, { shares: newShares, current_price: filledPrice });
    }
  }
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
  const refPrice = Number(input.price) || 0;
  if (!refPrice || refPrice <= 0) {
    return { status: 'invalid', error: 'a reference price is required' };
  }

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
        return await resumeBrokerOrder(sr, userId, intentRecord, input, brokerCred);
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
  };

  if (intentRecord) {
    await sr.entities.TradeIntent.update(intentRecord.id, intentData);
  } else {
    intentRecord = await sr.entities.TradeIntent.create(intentData);
  }

  // 1b. Market-data freshness guard (broker_paper and live). Stale prices ⇒ stale risk ⇒ reject.
  if (executionMode === 'live' || executionMode === 'broker_paper') {
    const freshness = await checkDataFreshness(sr, symbol, 5);
    if (!freshness.fresh) {
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'rejected',
        rejection_reason: `${freshness.reason} (age ${freshness.ageMinutes}m)`,
      });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: [`${freshness.reason} (age ${freshness.ageMinutes}m)`], symbol, side, requestedQty };
    }
  }

  // 1c. Fetch real account equity for risk sizing (broker_paper/live only).
  // FAIL-CLOSED: if account is unreachable or has no equity, reject before risk
  // evaluation — no order is submitted. The equity is passed to buildPortfolioSnapshot
  // so position/sector caps are computed against the real capital base, not stale
  // holding cache drift.
  let accountEquity = null;
  if (executionMode === 'live' || executionMode === 'broker_paper') {
    try {
      const acct = await getAlpacaAccount({ apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret, mode: brokerCred.mode });
      if (!acct || Number(acct.equity) <= 0) {
        await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: 'BROKER_ACCOUNT_INVALID: no equity' });
        return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: 'Broker account invalid', symbol, side, requestedQty };
      }
      accountEquity = Number(acct.equity);
    } catch (e) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `BROKER_UNREACHABLE: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: `Broker unreachable: ${e.message}`, symbol, side, requestedQty };
    }
  }

  // 2. RISK evaluation — deterministic, veto authority. DENIED ⇒ zero order, zero mutation.
  // Uses real broker account equity when available (broker_paper/live), falls back to
  // holdings-based equity for internal_paper mode.
  const snapshot = await buildPortfolioSnapshot(sr, userId, accountEquity);
  const limits = riskLimitsForProfile(user.trade_profile || 'balanced');
  const risk = evaluateRisk(
    { symbol, side, requested_quantity: requestedQty, limit_price: refPrice, price: refPrice, sector: input.sector, confidence: input.confidence },
    snapshot,
    limits,
    { killSwitch: !!user.kill_switch }
  );

  if (!risk.approved) {
    await sr.entities.TradeIntent.update(intentRecord.id, {
      status: 'rejected',
      rejection_reason: risk.reasons.join('; '),
      risk_snapshot: JSON.stringify({ reasons: risk.reasons, limits, snapshot: { totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, tradesToday: snapshot.tradesToday, dailyPnlPct: snapshot.dailyPnlPct } }),
    });
    return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: risk.reasons, symbol, side, requestedQty };
  }

  const approvedQty = risk.approvedQuantity;
  await sr.entities.TradeIntent.update(intentRecord.id, {
    status: 'risk_approved',
    requested_quantity: approvedQty,
    risk_snapshot: JSON.stringify({ reasons: risk.reasons, approvedQuantity: approvedQty, limits, snapshot: { totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, tradesToday: snapshot.tradesToday, dailyPnlPct: snapshot.dailyPnlPct } }),
    portfolio_snapshot: JSON.stringify({ totalEquity: snapshot.totalEquity, openPositions: snapshot.openPositions, sectorMap: snapshot.sectorMap }),
  });

  // 3. Execution by environment.
  if (executionMode === 'internal_paper' || executionMode === 'shadow_live') {
    // Simulated fill at the reference price. No broker submission.
    const filledQty = approvedQty;
    const filledPrice = refPrice;
    const venue = executionMode === 'shadow_live' ? 'shadow' : 'paper';
    const fillId = genId('fill');

    await sr.entities.TradeIntent.update(intentRecord.id, {
      status: 'filled',
      filled_quantity: filledQty,
      filled_avg_price: filledPrice,
    });

    // Idempotent fill insertion + settlement.
    await settleFill(sr, userId, {
      fillId,
      brokerOrderId: null,
      clientOrderId,
      tradeIntentId,
      symbol,
      side,
      filledQty,
      filledPrice,
      venue,
      executionMode,
      strategyId,
      decisionId: input.decision_id || null,
      input,
      intentRecord,
    });

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
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'submitted', broker: brokerCred.broker });
      const placed = await placeAlpacaOrder({
        ...creds, mode: alpacaMode, symbol, qty: approvedQty, side, client_order_id: clientOrderId,
        order_type: intentRecord.order_type || 'market',
        limit_price: intentRecord.limit_price,
        stop_price: intentRecord.stop_price,
        time_in_force: intentRecord.time_in_force || 'day',
      });
      brokerOrderId = placed.id;
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'accepted', broker_order_id: brokerOrderId, client_order_id: clientOrderId });
    } catch (e) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `BROKER_SUBMIT_ERROR: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: e.message, symbol, side, requestedQty: approvedQty };
    }
  }

  // 3c. Poll until terminal state. Record each incremental fill idempotently.
  return await pollAndSettle(sr, userId, intentRecord, creds, alpacaMode, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId);
}

// Poll a broker order until terminal state, recording incremental fills.
// A partial fill records its increment but keeps the intent partially_filled.
// Only when the order reaches filled/canceled/done_for_day/rejected do we settle.
async function pollAndSettle(sr, userId, intentRecord, creds, alpacaMode, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId) {
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
      // Alpaca's filled_avg_price is the VWAP of ALL fills so far. We record the
      // incremental qty at the cumulative VWAP — the fill_id encodes the cumulative
      // qty so it's idempotent.
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
        filled_price: cumulativeAvgPrice > 0 ? cumulativeAvgPrice : Number(input.price),
        notional: incrementalQty * (cumulativeAvgPrice > 0 ? cumulativeAvgPrice : Number(input.price)),
        commission: input.commission || 0,
        fees: input.fees || 0,
        slippage: 0,
        timestamp: nowIso(),
        venue,
        strategy_id: strategyId,
        decision_id: input.decision_id || null,
        execution_mode: executionMode,
        asset_class: input.asset_class || 'stocks',
      });

      if (recorded) {
        // Project the holding for this incremental fill.
        await projectHolding(sr, userId, symbol, side, incrementalQty, cumulativeAvgPrice > 0 ? cumulativeAvgPrice : Number(input.price), input);
      }

      lastFilledQty = cumulativeFilled;
      lastFilledPrice = cumulativeAvgPrice;
    }

    // Check for terminal state.
    if (status === 'filled' || status === 'done_for_day') {
      // Final settlement — record the Trade, AITradeDecision, and settle the intent.
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'filled',
        filled_quantity: cumulativeFilled,
        filled_avg_price: cumulativeAvgPrice,
      });
      return await settleFromFills(sr, userId, intentRecord, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId, cumulativeFilled, cumulativeAvgPrice, executionMode, venue);
    }

    if (isTerminalFailure(status)) {
      // Order terminated with partial or no fill. Settle what was filled (if anything).
      if (cumulativeFilled > 0) {
        await sr.entities.TradeIntent.update(intentRecord.id, {
          status: cumulativeFilled < intentRecord.requested_quantity ? 'partially_filled' : 'filled',
          filled_quantity: cumulativeFilled,
          filled_avg_price: cumulativeAvgPrice,
        });
        return await settleFromFills(sr, userId, intentRecord, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId, cumulativeFilled, cumulativeAvgPrice, executionMode, venue);
      }
      // No fill at all — reject.
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: status === 'rejected' ? 'rejected' : 'canceled',
        rejection_reason: `BROKER_${status.toUpperCase()}`,
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
async function resumeBrokerOrder(sr, userId, intentRecord, input, brokerCred) {
  if (!intentRecord.broker_order_id) {
    // Not yet submitted — re-enter the main flow from risk evaluation.
    return null; // caller will continue
  }
  const creds = { apiKey: brokerCred.api_key, secretKey: brokerCred.api_secret, mode: brokerCred.mode };
  const alpacaMode = intentRecord.execution_mode === 'live' ? 'live' : 'paper';
  return await pollAndSettle(
    sr, userId, intentRecord, creds, alpacaMode, input,
    intentRecord.broker_order_id, intentRecord.client_order_id, intentRecord.trade_intent_id, intentRecord.strategy_id
  );
}

// Final settlement — create the Trade record, AITradeDecision (if AI-driven), and settle the intent.
// This is called once the order reaches a terminal state. The fills are already recorded.
async function settleFromFills(sr, userId, intentRecord, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId, filledQty, filledPrice, executionMode, venue) {
  const symbol = intentRecord.symbol;
  const side = intentRecord.side;
  const totalValue = filledQty * filledPrice;

  // Realized P&L for sells.
  let realizedPnl = null;
  if (side === 'sell') {
    const holdings = await sr.entities.Holding.filter({ user_id: userId });
    const existing = holdings.find((h) => String(h.symbol).toUpperCase() === symbol);
    if (existing && existing.avg_price) {
      realizedPnl = (filledPrice - existing.avg_price) * filledQty;
    }
  }

  // Trade record (idempotent: check if a trade with this client_order_id already exists).
  const existingTrades = await sr.entities.Trade.filter({ user_id: userId, client_order_id: clientOrderId });
  let tradeId = existingTrades?.[0]?.id;
  if (!tradeId) {
    const trade = await sr.entities.Trade.create({
      user_id: userId,
      symbol,
      company_name: input.company_name || intentRecord.company_name || symbol,
      action: side,
      shares: filledQty,
      price: filledPrice,
      total_value: totalValue,
      notes: input.notes,
      ai_recommended: !!input.ai_recommended,
      broker_order_id: brokerOrderId,
      client_order_id: clientOrderId,
      filled_qty: filledQty,
      filled_avg_price: filledPrice,
      order_status: 'filled',
      commission: input.commission || 0,
      source: strategyId,
      realized_pnl: realizedPnl,
    });
    tradeId = trade.id;
  }

  // AITradeDecision (when AI-driven).
  let decisionId = input.decision_id || intentRecord.decision_id || null;
  if (input.recordDecision && !decisionId) {
    const d = await sr.entities.AITradeDecision.create({
      user_id: userId,
      symbol,
      company_name: input.company_name || intentRecord.company_name || symbol,
      sector: input.sector || intentRecord.sector || '',
      asset_class: input.asset_class || 'stocks',
      action: side,
      shares: filledQty,
      price: filledPrice,
      position_value: totalValue,
      confidence: input.confidence,
      target_price: input.target_price,
      stop_loss: input.stop_loss,
      reasoning: input.reasoning,
      status: 'executed',
      ml_score: input.ml_score,
      technical_score: input.technical_score,
      momentum_score: input.momentum_score,
      risk_score: input.risk_score,
      regime: input.regime || null,
    });
    decisionId = d.id;
  }

  // Settle the intent.
  await sr.entities.TradeIntent.update(intentRecord.id, {
    status: 'settled',
    filled_quantity: filledQty,
    filled_avg_price: filledPrice,
    commission: input.commission || 0,
    fees: input.fees || 0,
    realized_pnl: realizedPnl,
    decision_id: decisionId,
  });

  return {
    status: executionMode === 'live' ? 'filled' : 'paper_filled',
    intentId: intentRecord.id,
    trade_intent_id: tradeIntentId,
    brokerOrderId,
    clientOrderId,
    filled_qty: filledQty,
    filled_avg_price: filledPrice,
    total_value: totalValue,
    trade_id: tradeId,
    decision_id: decisionId,
    execution_mode: executionMode,
    symbol,
    action: side,
  };
}

// Settle a single-fill order (internal_paper / shadow_live).
async function settleFill(sr, userId, params) {
  const { fillId, brokerOrderId, clientOrderId, tradeIntentId, symbol, side, filledQty, filledPrice, venue, executionMode, strategyId, decisionId, input, intentRecord } = params;

  // Idempotent fill insertion.
  await recordFill(sr, {
    user_id: userId,
    fill_id: fillId,
    broker_order_id: brokerOrderId,
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
    decision_id: decisionId,
    execution_mode: executionMode,
    asset_class: input.asset_class || 'stocks',
  });

  // Project the holding.
  await projectHolding(sr, userId, symbol, side, filledQty, filledPrice, input);

  // Final settlement.
  return await settleFromFills(sr, userId, intentRecord, input, brokerOrderId, clientOrderId, tradeIntentId, strategyId, filledQty, filledPrice, executionMode, venue);
}

// Backward-compatible wrapper.
export async function settleTrade(base44, user, intent) {
  return executeIntent(base44, user, intent);
}

// Reconstruct positions from the immutable Fill ledger (day-zero rebuild).
export async function rebuildPositionsFromFills(sr, userId) {
  const fills = await sr.entities.Fill.filter({ user_id: userId });
  const positions = {};
  fills.forEach((f) => {
    const sym = String(f.symbol).toUpperCase();
    if (!positions[sym]) positions[sym] = { symbol: sym, shares: 0, cost: 0, fills: [] };
    const p = positions[sym];
    p.fills.push(f);
    if (f.side === 'buy') {
      p.shares += f.filled_quantity;
      p.cost += f.filled_quantity * f.filled_price;
    } else {
      p.shares -= f.filled_quantity;
      p.cost -= f.filled_quantity * f.filled_price;
    }
  });
  return Object.values(positions)
    .filter((p) => p.shares > 0.0001)
    .map((p) => ({ symbol: p.symbol, shares: p.shares, avg_price: p.cost / p.shares, fillCount: p.fills.length }));
}