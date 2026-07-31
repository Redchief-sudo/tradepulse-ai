// Canonical execution gateway — the single boundary every trading surface funnels through.
//
// State machine:
//   PROPOSED → RISK_APPROVED → SUBMITTED → ACCEPTED → (PARTIALLY_FILLED) → FILLED → SETTLED
//   Failure:  REJECTED | CANCELED | EXPIRED | FAILED
//
// A REJECTED broker order produces ZERO portfolio mutation. Paper, shadow-live, and live
// are explicit, isolated environments — a live failure is NEVER silently downgraded to paper.
// Accounting is derived from the immutable Fill ledger; Holding is a projection of it.

import { placeAlpacaOrder, getAlpacaOrder } from './alpaca.ts';
import { evaluateRisk, riskLimitsForProfile, buildPortfolioSnapshot, checkDataFreshness } from './riskEngine.ts';

const FILL_TIMEOUT_MS = 15000;
const POLL_INTERVAL_MS = 1000;

function isTerminalFailure(status) {
  return ['rejected', 'canceled', 'expired', 'replaced'].includes(status);
}
function nowIso() { return new Date().toISOString(); }
function genId(prefix) { return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`; }

// Poll an Alpaca order until it fills, fails, or the timeout elapses.
async function pollAlpacaFill(creds, orderId) {
  const start = Date.now();
  while (Date.now() - start < FILL_TIMEOUT_MS) {
    const order = await getAlpacaOrder(creds, orderId);
    const status = order.status;
    const filled_qty = Number(order.filled_qty) || 0;
    const filled_avg_price = Number(order.filled_avg_price) || 0;
    if (status === 'filled' || status === 'partially_filled' || status === 'done_for_day') {
      return { status, filled_qty, filled_avg_price };
    }
    if (isTerminalFailure(status)) {
      return { status, filled_qty, filled_avg_price };
    }
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }
  const order = await getAlpacaOrder(creds, orderId);
  return { status: order.status || 'pending', filled_qty: Number(order.filled_qty) || 0, filled_avg_price: Number(order.filled_avg_price) || 0 };
}

// Resolve the execution environment from the user profile + intent override.
function resolveExecutionMode(user, input) {
  if (input.execution_mode && ['paper', 'shadow_live', 'live'].includes(input.execution_mode)) {
    return input.execution_mode;
  }
  const brokerConnected = user.broker === 'alpaca' && user.broker_api_key && user.broker_api_secret;
  if (brokerConnected && user.broker_mode === 'live') return 'live';
  return 'paper';
}

// executeIntent — the canonical gateway.
// Returns { status: 'filled' | 'paper_filled' | 'rejected' | 'pending' | 'invalid', ... }
export async function executeIntent(base44, user, input) {
  const sr = base44.asServiceRole;
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

  const executionMode = resolveExecutionMode(user, input);
  const tradeIntentId = genId('tpi');
  const clientOrderId = genId('tp');
  const strategyId = input.source || input.strategy_id || 'manual';
  const signalTs = input.signal_timestamp || nowIso();

  // 1. PROPOSED — persist the canonical intent.
  const intentRecord = await sr.entities.TradeIntent.create({
    trade_intent_id: tradeIntentId,
    strategy_id: strategyId,
    decision_id: input.decision_id || null,
    asset_class: input.asset_class || 'stocks',
    symbol,
    native_asset_id: input.native_asset_id || symbol,
    broker: user.broker || (executionMode === 'paper' ? 'paper' : 'external'),
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
  });

  // 1b. Market-data freshness guard (LIVE only). Stale prices ⇒ stale risk ⇒ reject.
  if (executionMode === 'live') {
    const freshness = await checkDataFreshness(sr, symbol, 5);
    if (!freshness.fresh) {
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: 'rejected',
        rejection_reason: `${freshness.reason} (age ${freshness.ageMinutes}m)`,
      });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, reasons: [`${freshness.reason} (age ${freshness.ageMinutes}m)`], symbol, side, requestedQty };
    }
  }

  // 2. RISK evaluation — deterministic, veto authority. DENIED ⇒ zero order, zero mutation.
  const snapshot = await buildPortfolioSnapshot(sr);
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
  let brokerOrderId = null;
  let orderStatus = executionMode === 'live' ? 'pending' : 'paper_filled';
  let filledQty = approvedQty;
  let filledPrice = refPrice;
  const venue = executionMode === 'shadow_live' ? 'shadow' : (executionMode === 'live' ? 'alpaca' : 'paper');

  if (executionMode === 'live') {
    const creds = { apiKey: user.broker_api_key, secretKey: user.broker_api_secret, mode: 'live' };
    let placed;
    try {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'submitted', broker: 'alpaca' });
      placed = await placeAlpacaOrder({ ...creds, symbol, qty: approvedQty, side, client_order_id: clientOrderId });
    } catch (e) {
      // Live submission failed — REJECTED. NEVER silently downgrade to paper.
      await sr.entities.TradeIntent.update(intentRecord.id, { status: 'rejected', rejection_reason: `BROKER_SUBMIT_ERROR: ${e.message}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, error: e.message, symbol, side, requestedQty: approvedQty };
    }
    brokerOrderId = placed.id;
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'accepted', broker_order_id: brokerOrderId, client_order_id: clientOrderId });

    const result = await pollAlpacaFill(creds, brokerOrderId);
    orderStatus = result.status;
    if (result.filled_qty > 0) {
      filledQty = result.filled_qty;
      filledPrice = result.filled_avg_price > 0 ? result.filled_avg_price : refPrice;
      await sr.entities.TradeIntent.update(intentRecord.id, {
        status: result.status === 'partially_filled' ? 'partially_filled' : 'filled',
        filled_quantity: filledQty,
        filled_avg_price: filledPrice,
      });
    } else if (isTerminalFailure(orderStatus)) {
      await sr.entities.TradeIntent.update(intentRecord.id, { status: orderStatus === 'rejected' ? 'rejected' : 'canceled', rejection_reason: `BROKER_${orderStatus.toUpperCase()}` });
      return { status: 'rejected', intentId: intentRecord.id, trade_intent_id: tradeIntentId, orderStatus, brokerOrderId, symbol, side, requestedQty: approvedQty };
    } else {
      // Pending / timeout — order may still fill later; do not settle now.
      return { status: 'pending', intentId: intentRecord.id, trade_intent_id: tradeIntentId, orderStatus, brokerOrderId, symbol, side, requestedQty: approvedQty };
    }
  } else {
    // paper or shadow_live — simulated fill at the reference price. No broker submission.
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'filled', filled_quantity: filledQty, filled_avg_price: filledPrice });
  }

  if (!filledPrice || filledPrice <= 0) {
    await sr.entities.TradeIntent.update(intentRecord.id, { status: 'failed', rejection_reason: 'NO_FILL_PRICE' });
    return { status: 'invalid', intentId: intentRecord.id, error: 'no fill price available' };
  }

  // 4. SETTLED — write the immutable Fill ledger, then derive Holding + Trade from it.
  const slippage = executionMode === 'live' ? (filledPrice - refPrice) * filledQty * (side === 'buy' ? 1 : -1) : 0;
  const fillId = executionMode === 'live' ? (brokerOrderId || genId('fill')) : genId('fill');
  await sr.entities.Fill.create({
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
    slippage,
    timestamp: nowIso(),
    venue,
    strategy_id: strategyId,
    decision_id: input.decision_id || null,
    execution_mode: executionMode,
    asset_class: input.asset_class || 'stocks',
  });

  const totalValue = filledQty * filledPrice;
  let realizedPnl = null;
  const holdings = await sr.entities.Holding.list();
  const existing = holdings.find((h) => String(h.symbol).toUpperCase() === symbol);
  if (side === 'sell' && existing && existing.avg_price) {
    realizedPnl = (filledPrice - existing.avg_price) * filledQty;
  }

  // Trade record (existing ledger / UI history), now with fill-level accounting.
  const trade = await sr.entities.Trade.create({
    symbol,
    company_name: input.company_name || symbol,
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
    order_status: orderStatus,
    commission: input.commission || 0,
    source: strategyId,
    realized_pnl: realizedPnl,
  });

  // Derive the Holding cache from the fill (Holding is a projection of the Fill ledger).
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
        symbol,
        company_name: input.company_name || symbol,
        shares: filledQty,
        avg_price: filledPrice,
        current_price: filledPrice,
        sector: input.sector || '',
        day_change_percent: 0,
        stop_loss: input.stop_loss,
        target_price: input.target_price,
      });
    }
  } else {
    if (existing) {
      const newShares = existing.shares - filledQty;
      if (newShares <= 0) await sr.entities.Holding.delete(existing.id);
      else await sr.entities.Holding.update(existing.id, { shares: newShares, current_price: filledPrice });
    }
  }

  // AITradeDecision (when AI-driven).
  let decisionId = input.decision_id || null;
  if (input.recordDecision) {
    const d = await sr.entities.AITradeDecision.create({
      symbol,
      company_name: input.company_name || symbol,
      sector: input.sector || '',
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
    });
    decisionId = d.id;
    if (decisionId) await sr.entities.TradeIntent.update(intentRecord.id, { decision_id: decisionId });
  }

  await sr.entities.TradeIntent.update(intentRecord.id, {
    status: 'settled',
    filled_quantity: filledQty,
    filled_avg_price: filledPrice,
    commission: input.commission || 0,
    fees: input.fees || 0,
    slippage,
    realized_pnl: realizedPnl,
  });

  return {
    status: executionMode === 'live' ? 'filled' : 'paper_filled',
    orderStatus,
    intentId: intentRecord.id,
    trade_intent_id: tradeIntentId,
    brokerOrderId,
    clientOrderId,
    filled_qty: filledQty,
    filled_avg_price: filledPrice,
    total_value: totalValue,
    trade_id: trade.id,
    decision_id: decisionId,
    execution_mode: executionMode,
    symbol,
    action: side,
  };
}

// Backward-compatible wrapper: existing callers (runAutonomousScanCycle,
// runStopLossCycle, UI surfaces) still call settleTrade() and get the legacy
// result shape. It now delegates to the full state-machine gateway.
export async function settleTrade(base44, user, intent) {
  return executeIntent(base44, user, intent);
}

// Reconstruct the entire position set from the immutable Fill ledger (day-zero rebuild).
// Proves the ledger is authoritative — the portfolio can be rebuilt from fills alone.
export async function rebuildPositionsFromFills(sr) {
  const fills = await sr.entities.Fill.list('-created_date', 5000);
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