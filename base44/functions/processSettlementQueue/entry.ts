// Settlement Processor — the SINGLE WRITER for all financial projections.
//
// No execution request, order reconciliation worker, UI request, or other
// function may directly mutate CashEntry, PositionLot, Holding, realized P&L,
// or final Trade settlement. Instead they create a SettlementEvent and this
// processor handles the projection sequentially.
//
// For each event:
// 1. Claim the event (atomic with lease — crash recovery via stale lease)
// 2. Idempotent check (has this event already been projected?)
// 3. Lot accounting (create PositionLot for buy, close lots FIFO for sell)
// 4. Cash accounting (debit for buy, credit for sell — internal paper only)
// 5. Holding projection (derive from open PositionLots)
// 6. Trade summary (create/update Trade — idempotent by client_order_id)
// 7. AITradeDecision (create once for AI-driven trades)
// 8. TradeIntent update (reservation state machine, settlement state)
// 9. Financial integrity verification
// 10. Mark completed
//
// If integrity fails: mark system financial_integrity_blocked, disable new
// entries, allow only protective exits, create critical AuditEvent.

import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { recordBuySettlement, recordSellSettlement, getCashBalance } from '../../shared/cashLedger.ts';
import { updateSessionState, SESSION_STATES } from '../../shared/sessionState.ts';
import { nowIso, genId, parseClosureFills } from '../../shared/lotAccounting.ts';

const STALE_LEASE_MS = 5 * 60 * 1000; // 5 minutes

// Audit helper
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
    });
  } catch (e) { /* non-fatal */ }
}

// Claim an event for processing — atomic with lease.
async function claimEvent(sr, userId, event, workerId) {
  await sr.entities.SettlementEvent.update(event.id, {
    status: 'processing',
    processing_owner: workerId,
    processing_started_at: nowIso(),
  });
  // Re-read to verify we won the claim
  const reloaded = await sr.entities.SettlementEvent.filter({ user_id: userId, event_id: event.event_id });
  if (reloaded[0] && reloaded[0].processing_owner === workerId) {
    return { claimed: true, event: reloaded[0] };
  }
  return { claimed: false, reason: 'LOST_RACE' };
}

// Idempotent check — has this event already been projected?
async function checkAlreadyProjected(sr, userId, event) {
  if (event.side === 'buy') {
    const lots = await sr.entities.PositionLot.filter({ user_id: userId, originating_fill_id: event.event_id });
    return lots.length > 0;
  }
  // For sells: check if event_id is in any lot's closure_fill_ids
  const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: event.symbol });
  for (const lot of lots) {
    const closures = parseClosureFills(lot.closure_fill_ids);
    if (closures.some((c) => c.fill_id === event.event_id)) return true;
  }
  return false;
}

// Create a PositionLot for a buy fill
async function createPositionLot(sr, userId, event) {
  await sr.entities.PositionLot.create({
    user_id: userId,
    portfolio_id: event.portfolio_id || null,
    lot_id: genId('lot'),
    symbol: event.symbol,
    company_name: event.company_name || event.symbol,
    sector: event.sector || '',
    asset_class: event.asset_class || 'stocks',
    originating_fill_id: event.event_id,
    quantity_opened: event.quantity,
    quantity_remaining: event.quantity,
    acquisition_price: event.price,
    acquisition_timestamp: event.occurred_at || nowIso(),
    status: 'open',
    realized_pnl: 0,
    closure_fill_ids: '[]',
    cost_basis_method: 'fifo',
    provenance_source: 'app_fill',
    provenance_quality: 'verified',
  });
}

// Close lots FIFO for a sell fill — returns realized P&L
async function closeLotsFifo(sr, userId, event) {
  const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: event.symbol });
  const openLots = lots
    .filter((l) => l.status === 'open' || l.status === 'partially_closed')
    .sort((a, b) => new Date(a.acquisition_timestamp) - new Date(b.acquisition_timestamp));

  const availableQty = openLots.reduce((s, l) => s + l.quantity_remaining, 0);
  if (event.quantity > availableQty + 0.0001) {
    throw new Error(`SELL_FILL_EXCEEDS_ACCOUNTED_POSITION: requested ${event.quantity}, available ${availableQty} for ${event.symbol}`);
  }

  let remainingQty = event.quantity;
  let realizedPnl = 0;

  for (const lot of openLots) {
    if (remainingQty <= 0.0001) break;
    const closeQty = Math.min(lot.quantity_remaining, remainingQty);
    const lotPnl = (event.price - lot.acquisition_price) * closeQty;
    realizedPnl += lotPnl;

    const newRemaining = lot.quantity_remaining - closeQty;
    const closureAllocations = parseClosureFills(lot.closure_fill_ids);
    closureAllocations.push({ fill_id: event.event_id, qty: closeQty });

    await sr.entities.PositionLot.update(lot.id, {
      quantity_remaining: newRemaining,
      status: newRemaining <= 0.0001 ? 'closed' : 'partially_closed',
      realized_pnl: (lot.realized_pnl || 0) + lotPnl,
      closure_fill_ids: JSON.stringify(closureAllocations),
    });
    remainingQty -= closeQty;
  }
  return realizedPnl;
}

// Derive Holding from open PositionLots — the lot ledger is the source of truth.
async function updateHoldingProjection(sr, userId, event) {
  const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: event.symbol });
  const openLots = lots.filter((l) => l.status === 'open' || l.status === 'partially_closed');

  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const existing = holdings.find((h) => String(h.symbol).toUpperCase() === event.symbol.toUpperCase());

  if (openLots.length === 0) {
    if (existing) await sr.entities.Holding.delete(existing.id);
    return;
  }

  const totalShares = openLots.reduce((s, l) => s + l.quantity_remaining, 0);
  const totalCost = openLots.reduce((s, l) => s + l.quantity_remaining * l.acquisition_price, 0);
  const avgPrice = totalShares > 0 ? totalCost / totalShares : 0;

  if (existing) {
    await sr.entities.Holding.update(existing.id, {
      shares: totalShares,
      avg_price: avgPrice,
      current_price: event.price,
    });
  } else {
    await sr.entities.Holding.create({
      user_id: userId,
      portfolio_id: event.portfolio_id || null,
      symbol: event.symbol,
      company_name: event.company_name || event.symbol,
      shares: totalShares,
      avg_price: avgPrice,
      current_price: event.price,
      sector: event.sector || '',
      day_change_percent: 0,
      asset_class: event.asset_class || 'stocks',
    });
  }
}

// Create or update Trade record — idempotent by client_order_id.
// Uses cumulative fill data from the Fill ledger for the order summary.
async function createOrUpdateTrade(sr, userId, event, realizedPnl) {
  if (!event.client_order_id) return null;

  const existingTrades = await sr.entities.Trade.filter({ user_id: userId, client_order_id: event.client_order_id });
  const fills = await sr.entities.Fill.filter({ user_id: userId, client_order_id: event.client_order_id });
  const cumulativeQty = fills.reduce((s, f) => s + (f.filled_quantity || 0), 0);
  const totalNotional = fills.reduce((s, f) => s + (f.filled_quantity || 0) * (f.filled_price || 0), 0);
  const avgPrice = cumulativeQty > 0 ? totalNotional / cumulativeQty : event.price;

  if (existingTrades.length > 0) {
    await sr.entities.Trade.update(existingTrades[0].id, {
      shares: cumulativeQty,
      price: avgPrice,
      total_value: cumulativeQty * avgPrice,
      filled_qty: cumulativeQty,
      filled_avg_price: avgPrice,
      fill_count: fills.length,
      last_fill_at: nowIso(),
      realized_pnl: event.side === 'sell' ? realizedPnl : existingTrades[0].realized_pnl,
    });
    return existingTrades[0].id;
  }

  const trade = await sr.entities.Trade.create({
    user_id: userId,
    portfolio_id: event.portfolio_id || null,
    symbol: event.symbol,
    company_name: event.company_name || event.symbol,
    action: event.side,
    shares: cumulativeQty,
    price: avgPrice,
    total_value: cumulativeQty * avgPrice,
    ai_recommended: !!event.decision_id,
    broker_order_id: event.broker_order_id,
    client_order_id: event.client_order_id,
    filled_qty: cumulativeQty,
    filled_avg_price: avgPrice,
    order_status: 'filled',
    commission: event.commission || 0,
    source: event.strategy_id,
    realized_pnl: event.side === 'sell' ? realizedPnl : null,
    record_type: 'order_summary',
    fill_count: fills.length,
    first_fill_at: event.occurred_at,
    last_fill_at: nowIso(),
  });
  return trade.id;
}

// Create AITradeDecision for AI-driven trades — idempotent by decision_id.
async function createAiDecisionIfApplicable(sr, userId, event) {
  const intents = await sr.entities.TradeIntent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  const intent = intents[0];
  if (!intent || intent.ml_score == null) return; // not AI-driven

  // If the intent already has a decision_id, the decision was already created
  if (intent.decision_id) return;

  await sr.entities.AITradeDecision.create({
    user_id: userId,
    portfolio_id: event.portfolio_id || null,
    symbol: event.symbol,
    company_name: event.company_name || intent.company_name || event.symbol,
    sector: event.sector || intent.sector || '',
    asset_class: event.asset_class || 'stocks',
    action: event.side,
    shares: event.quantity,
    price: event.price,
    position_value: event.quantity * event.price,
    confidence: intent.confidence,
    target_price: intent.target_price,
    stop_loss: intent.stop_loss,
    reasoning: intent.reasoning,
    status: 'executed',
    ml_score: intent.ml_score,
    technical_score: intent.technical_score,
    momentum_score: intent.momentum_score,
    risk_score: intent.risk_score,
  });
}

// Update TradeIntent — reservation state machine + settlement state.
async function updateTradeIntent(sr, userId, event, realizedPnl) {
  const intents = await sr.entities.TradeIntent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  if (!intents[0]) return;
  const intent = intents[0];

  const fills = await sr.entities.Fill.filter({ user_id: userId, client_order_id: event.client_order_id });
  const cumulativeQty = fills.reduce((s, f) => s + (f.filled_quantity || 0), 0);
  const totalNotional = fills.reduce((s, f) => s + (f.filled_quantity || 0) * (f.filled_price || 0), 0);
  const avgPrice = cumulativeQty > 0 ? totalNotional / cumulativeQty : 0;

  const patch = {
    filled_quantity: cumulativeQty,
    filled_avg_price: avgPrice,
    settlement_state: 'settled',
    settlement_version: (intent.settlement_version || 0) + 1,
  };

  // Reservation state machine (buys only)
  if (event.side === 'buy' && intent.reserved_cash > 0) {
    const fillCost = event.notional + (event.commission || 0) + (event.fees || 0);
    const newConsumed = (intent.consumed_cash || 0) + fillCost;
    patch.consumed_cash = newConsumed;
    patch.reservation_status = newConsumed >= intent.reserved_cash ? 'consumed' : 'partially_consumed';
  }

  // Accumulate realized P&L for sells
  if (event.side === 'sell' && realizedPnl != null) {
    patch.realized_pnl = (intent.realized_pnl || 0) + realizedPnl;
  }

  await sr.entities.TradeIntent.update(intent.id, patch);
}

// Financial integrity verification — checks invariants after projection.
async function verifyIntegrity(sr, userId, event) {
  const errors = [];

  // 1. Cash is not negative (internal paper mode only)
  if (event.execution_mode === 'internal_paper' || event.execution_mode === 'shadow_live') {
    const balance = await getCashBalance(sr, userId);
    if (balance < -0.01) {
      errors.push(`CASH_NEGATIVE: balance ${balance.toFixed(2)}`);
    }
  }

  // 2. Holding quantity equals sum of open PositionLots
  const lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: event.symbol });
  const openLots = lots.filter((l) => l.status === 'open' || l.status === 'partially_closed');
  const lotQty = openLots.reduce((s, l) => s + l.quantity_remaining, 0);

  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const holding = holdings.find((h) => String(h.symbol).toUpperCase() === event.symbol.toUpperCase());
  const holdingQty = holding ? holding.shares : 0;

  if (Math.abs(lotQty - holdingQty) > 0.001) {
    errors.push(`HOLDING_LOT_MISMATCH: holding ${holdingQty}, lots ${lotQty} for ${event.symbol}`);
  }

  // 3. Reservation invariants (for the intent)
  const intents = await sr.entities.TradeIntent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  const intent = intents[0];
  if (intent && intent.reserved_cash > 0) {
    if ((intent.consumed_cash || 0) > intent.reserved_cash + 0.01) {
      errors.push(`CONSUMED_EXCEEDS_RESERVED: consumed ${intent.consumed_cash}, reserved ${intent.reserved_cash}`);
    }
  }

  // 4. One broker fill maps to one SettlementEvent
  if (event.broker_fill_id) {
    const dupes = await sr.entities.SettlementEvent.filter({ user_id: userId, broker_fill_id: event.broker_fill_id });
    const completed = dupes.filter((e) => e.status === 'completed' && e.id !== event.id);
    if (completed.length > 0) {
      errors.push(`DUPLICATE_BROKER_FILL: ${event.broker_fill_id} already completed in ${completed.length} other event(s)`);
    }
  }

  return errors.length > 0 ? { ok: false, reason: errors.join('; ') } : { ok: true };
}

// Process a single settlement event — the core projection logic.
async function processEvent(sr, userId, event) {
  // 1. Idempotent check
  if (await checkAlreadyProjected(sr, userId, event)) {
    await sr.entities.SettlementEvent.update(event.id, {
      status: 'completed',
      completed_at: nowIso(),
      integrity_verified: true,
    });
    return { symbol: event.symbol, side: event.side, skipped: true, reason: 'already_projected' };
  }

  // 2. Lot accounting
  let realizedPnl = null;
  if (event.side === 'buy') {
    await createPositionLot(sr, userId, event);
  } else {
    realizedPnl = await closeLotsFifo(sr, userId, event);
  }

  // 3. Cash accounting (internal paper / shadow mode only)
  if (event.execution_mode === 'internal_paper' || event.execution_mode === 'shadow_live') {
    const notional = event.quantity * event.price;
    if (event.side === 'buy') {
      await recordBuySettlement(sr, userId, {
        symbol: event.symbol, notional,
        commission: event.commission || 0, fees: event.fees || 0,
        trade_intent_id: event.trade_intent_id, fill_id: event.event_id,
        portfolio_id: event.portfolio_id,
      });
    } else {
      await recordSellSettlement(sr, userId, {
        symbol: event.symbol, notional,
        commission: event.commission || 0, fees: event.fees || 0,
        trade_intent_id: event.trade_intent_id, fill_id: event.event_id,
        portfolio_id: event.portfolio_id,
      });
    }
  }

  // 4. Holding projection
  await updateHoldingProjection(sr, userId, event);

  // 5. Trade summary
  await createOrUpdateTrade(sr, userId, event, realizedPnl);

  // 6. AITradeDecision
  await createAiDecisionIfApplicable(sr, userId, event);

  // 7. TradeIntent update (reservation state + settlement state)
  await updateTradeIntent(sr, userId, event, realizedPnl);

  // 8. Financial integrity verification
  const integrity = await verifyIntegrity(sr, userId, event);
  if (!integrity.ok) {
    throw new Error(`INTEGRITY_VIOLATION: ${integrity.reason}`);
  }

  // 9. Mark completed
  await sr.entities.SettlementEvent.update(event.id, {
    status: 'completed',
    completed_at: nowIso(),
    integrity_verified: true,
    realized_pnl: realizedPnl,
  });

  return { symbol: event.symbol, side: event.side, quantity: event.quantity, price: event.price, realized_pnl: realizedPnl };
}

export default async function(req) {
  const base44 = createClientFromRequest(req);
  const user = await base44.auth.me();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sr = base44.asServiceRole;
  const userId = user.id;
  const workerId = `settlement-${crypto.randomUUID()}`;
  const now = Date.now();

  // Find pending or stale processing events
  const allEvents = await sr.entities.SettlementEvent.filter({ user_id: userId });
  const processable = allEvents
    .filter((e) =>
      e.status === 'pending' ||
      (e.status === 'processing' && e.processing_started_at &&
        now - new Date(e.processing_started_at).getTime() > STALE_LEASE_MS)
    )
    .sort((a, b) => new Date(a.occurred_at) - new Date(b.occurred_at));

  const results = [];
  for (const event of processable) {
    const claimResult = await claimEvent(sr, userId, event, workerId);
    if (!claimResult.claimed) continue;

    try {
      const result = await processEvent(sr, userId, claimResult.event);
      results.push({ event_id: event.event_id, status: 'completed', ...result });
    } catch (error) {
      await sr.entities.SettlementEvent.update(claimResult.event.id, {
        status: 'failed',
        error: error.message,
        completed_at: nowIso(),
      });
      results.push({ event_id: event.event_id, status: 'failed', error: error.message });

      await audit(sr, userId, 'settlement_failed', 'error', {
        correlation_id: event.trade_intent_id,
        entity_type: 'SettlementEvent',
        entity_id: event.id,
        message: `Settlement failed for ${event.symbol} ${event.side} ${event.quantity}: ${error.message}`,
      });

      // Financial integrity violation — block the system
      if (error.message.startsWith('INTEGRITY_VIOLATION')) {
        try {
          await updateSessionState(sr, userId, SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED, error.message);
          await audit(sr, userId, 'financial_integrity_blocked', 'critical', {
            message: `Financial integrity blocked: ${error.message}`,
          });
        } catch (e) { /* non-fatal */ }
      }
    }
  }

  return Response.json({ ok: true, processed: results.length, results });
}