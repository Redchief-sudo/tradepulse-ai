// Settlement Processor — the SINGLE WRITER for all financial projections.
//
// No execution request, order reconciliation worker, UI request, or other
// function may directly mutate CashEntry, PositionLot, Holding, realized P&L,
// or final Trade settlement. Instead they create a SettlementEvent and this
// processor handles the projection sequentially.
//
// For each event:
// 1. Serialize processors per user, then claim each event with a recoverable lease
// 2. Resume after the last durable stage checkpoint
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
import { classifySettlementFailure, deriveOrderSettlementSummary, isSettlementProcessable, runSettlementStages, selectLeaseWinner, summarizeSettlementBatch } from '../../shared/settlementState.ts';

const STALE_LEASE_MS = 5 * 60 * 1000; // 5 minutes
const PROCESSOR_LEASE_MS = 10 * 60 * 1000;

async function acquireProcessorLease(sr, userId, workerId) {
  const acquiredAt = nowIso();
  const lockKey = `settlement-processor-${userId}`;
  const ownLock = await sr.entities.ScanLock.create({
    user_id: userId,
    lock_key: lockKey,
    owner_token: workerId,
    acquired_at: acquiredAt,
    heartbeat_at: acquiredAt,
    expires_at: new Date(Date.now() + PROCESSOR_LEASE_MS).toISOString(),
  });
  const locks = await sr.entities.ScanLock.filter({ user_id: userId, lock_key: lockKey });
  const activeLocks = locks.filter((lock) => new Date(lock.expires_at).getTime() > Date.now());
  const winner = selectLeaseWinner(activeLocks);
  if (!winner || winner.owner_token !== workerId) {
    await sr.entities.ScanLock.delete(ownLock.id).catch(() => {});
    return null;
  }
  return ownLock;
}

async function renewProcessorLease(sr, lock) {
  const heartbeatAt = nowIso();
  await sr.entities.ScanLock.update(lock.id, {
    heartbeat_at: heartbeatAt,
    expires_at: new Date(Date.now() + PROCESSOR_LEASE_MS).toISOString(),
  });
}

async function ownsProcessorLease(sr, userId, lock) {
  const locks = await sr.entities.ScanLock.filter({ user_id: userId, lock_key: lock.lock_key });
  const activeLocks = locks.filter((candidate) => new Date(candidate.expires_at).getTime() > Date.now());
  return selectLeaseWinner(activeLocks)?.owner_token === lock.owner_token;
}

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

// Create a PositionLot for a buy fill
async function createPositionLot(sr, userId, event) {
  const existing = await sr.entities.PositionLot.filter({ user_id: userId, originating_fill_id: event.event_id });
  if (existing.length > 0) return;
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
  let alreadyAllocated = 0;
  let realizedPnl = 0;
  for (const lot of lots) {
    for (const allocation of parseClosureFills(lot.closure_fill_ids)) {
      if (allocation.fill_id === event.event_id) {
        alreadyAllocated += Number(allocation.qty) || 0;
        realizedPnl += (event.price - lot.acquisition_price) * (Number(allocation.qty) || 0);
      }
    }
  }
  if (alreadyAllocated > event.quantity + 0.0001) {
    throw new Error(`INTEGRITY_VIOLATION: SELL_EVENT_OVERALLOCATED ${alreadyAllocated} > ${event.quantity}`);
  }

  const openLots = lots
    .filter((l) => l.status === 'open' || l.status === 'partially_closed')
    .sort((a, b) => new Date(a.acquisition_timestamp) - new Date(b.acquisition_timestamp));

  const remainingRequired = event.quantity - alreadyAllocated;
  const availableQty = openLots.reduce((s, l) => s + l.quantity_remaining, 0);
  if (remainingRequired > availableQty + 0.0001) {
    throw new Error(`SELL_FILL_EXCEEDS_ACCOUNTED_POSITION: remaining ${remainingRequired}, available ${availableQty} for ${event.symbol}`);
  }

  let remainingQty = remainingRequired;

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
  const intents = await sr.entities.TradeIntent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  const intent = intents[0] || {};
  const cumulativeQty = fills.reduce((s, f) => s + (f.filled_quantity || 0), 0);
  const totalNotional = fills.reduce((s, f) => s + (f.filled_quantity || 0) * (f.filled_price || 0), 0);
  const avgPrice = cumulativeQty > 0 ? totalNotional / cumulativeQty : event.price;
  const summary = deriveOrderSettlementSummary(intent, fills);

  if (existingTrades.length > 0) {
    await sr.entities.Trade.update(existingTrades[0].id, {
      shares: cumulativeQty,
      price: avgPrice,
      total_value: cumulativeQty * avgPrice,
      filled_qty: cumulativeQty,
      filled_avg_price: avgPrice,
      fill_count: fills.length,
      order_status: summary.orderStatus,
      first_fill_at: summary.firstFillAt,
      last_fill_at: summary.lastFillAt,
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
    order_status: summary.orderStatus,
    commission: event.commission || 0,
    source: event.strategy_id,
    realized_pnl: event.side === 'sell' ? realizedPnl : null,
    record_type: 'order_summary',
    fill_count: fills.length,
    first_fill_at: summary.firstFillAt,
    last_fill_at: summary.lastFillAt,
  });
  return trade.id;
}

// Create one AITradeDecision per logical TradeIntent. Partial fills remain
// fill-level execution evidence but must not become independent learning samples.
async function createAiDecisionIfApplicable(sr, userId, event) {
  const intents = await sr.entities.TradeIntent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  const intent = intents[0];
  if (!intent || intent.ml_score == null) return; // not AI-driven

  const fills = await sr.entities.Fill.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  const sameSideFills = fills.filter((fill) => fill.side === event.side);
  const cumulativeQty = sameSideFills.reduce((sum, fill) => sum + (Number(fill.filled_quantity) || 0), 0);
  const cumulativeNotional = sameSideFills.reduce((sum, fill) => sum + (Number(fill.filled_quantity) || 0) * (Number(fill.filled_price) || 0), 0);
  const price = cumulativeQty > 0 ? cumulativeNotional / cumulativeQty : event.price;
  let existing = await sr.entities.AITradeDecision.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
  if (!existing.length) {
    const intentEvents = await sr.entities.SettlementEvent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
    const eventIds = new Set(intentEvents.map((candidate) => candidate.event_id));
    const legacyDecisions = await sr.entities.AITradeDecision.filter({ user_id: userId });
    existing = legacyDecisions.filter((decision) => eventIds.has(decision.settlement_event_id));
  }
  if (existing.length > 0) {
    await sr.entities.AITradeDecision.update(existing[0].id, {
      trade_intent_id: event.trade_intent_id,
      shares: cumulativeQty,
      price,
      position_value: cumulativeNotional,
      regime: existing[0].regime || event.regime || intent.regime || null,
    });
    return;
  }

  await sr.entities.AITradeDecision.create({
    settlement_event_id: event.event_id,
    trade_intent_id: event.trade_intent_id,
    user_id: userId,
    portfolio_id: event.portfolio_id || null,
    symbol: event.symbol,
    company_name: event.company_name || intent.company_name || event.symbol,
    sector: event.sector || intent.sector || '',
    asset_class: event.asset_class || 'stocks',
    action: event.side,
    shares: cumulativeQty,
    price,
    position_value: cumulativeNotional,
    confidence: intent.confidence,
    target_price: intent.target_price,
    stop_loss: intent.stop_loss,
    reasoning: intent.reasoning,
    status: 'executed',
    ml_score: intent.ml_score,
    technical_score: intent.technical_score,
    momentum_score: intent.momentum_score,
    risk_score: intent.risk_score,
    regime: event.regime || intent.regime || null,
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
  const summary = deriveOrderSettlementSummary(intent, fills);

  const patch = {
    filled_quantity: cumulativeQty,
    filled_avg_price: avgPrice,
    settlement_state: summary.settlementState,
    settlement_version: (intent.settlement_version || 0) + 1,
  };

  // Reservation state machine (buys only)
  if (event.side === 'buy' && intent.reserved_cash > 0) {
    const consumed = fills.reduce((sum, fill) => sum +
      (Number(fill.notional) || (Number(fill.filled_quantity) || 0) * (Number(fill.filled_price) || 0)) +
      (Number(fill.commission) || 0) + (Number(fill.fees) || 0), 0);
    patch.consumed_cash = consumed;
    patch.reservation_status = consumed >= intent.reserved_cash ? 'consumed' : 'partially_consumed';
  }

  // Accumulate realized P&L for sells
  if (event.side === 'sell' && realizedPnl != null) {
    const settlementEvents = await sr.entities.SettlementEvent.filter({ user_id: userId, trade_intent_id: event.trade_intent_id });
    patch.realized_pnl = settlementEvents
      .filter((candidate) => candidate.lot_projected)
      .reduce((sum, candidate) => sum + (Number(candidate.realized_pnl) || 0), 0);
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

// Apply intent-owned holding metadata only after every financial projection and
// integrity check has succeeded. This makes adaptive exit changes recoverable:
// a failure leaves the event incomplete and the scheduled processor retries it.
async function projectPostSettlementMetadata(sr, userId, event) {
  if (event.side !== 'sell') return;
  const intents = await sr.entities.TradeIntent.filter({
    user_id: userId,
    trade_intent_id: event.trade_intent_id,
  });
  const intent = intents[0];
  if (!intent || intent.stop_loss == null || intent.target_price == null) return;

  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const holding = holdings.find((candidate) =>
    String(candidate.symbol).toUpperCase() === String(event.symbol).toUpperCase()
  );
  if (!holding) return;

  await sr.entities.Holding.update(holding.id, {
    stop_loss: intent.stop_loss,
    target_price: intent.target_price,
  });
}

// Process a single settlement event — the core projection logic.
async function processEvent(sr, userId, event, beforeStage) {
  const handlers = {
    projectLot: async (state) => {
      const realizedPnl = state.side === 'buy' ? null : await closeLotsFifo(sr, userId, state);
      if (state.side === 'buy') await createPositionLot(sr, userId, state);
      return { patch: { realized_pnl: realizedPnl } };
    },
    projectCash: async (state) => {
      if (state.execution_mode !== 'internal_paper' && state.execution_mode !== 'shadow_live') return;
      const params = {
        symbol: state.symbol,
        notional: state.quantity * state.price,
        commission: state.commission || 0,
        fees: state.fees || 0,
        trade_intent_id: state.trade_intent_id,
        fill_id: state.event_id,
        portfolio_id: state.portfolio_id,
      };
      if (state.side === 'buy') await recordBuySettlement(sr, userId, params);
      else await recordSellSettlement(sr, userId, params);
    },
    projectHolding: async (state) => updateHoldingProjection(sr, userId, state),
    projectTrade: async (state) => createOrUpdateTrade(sr, userId, state, state.realized_pnl),
    projectDecision: async (state) => createAiDecisionIfApplicable(sr, userId, state),
    projectIntent: async (state) => updateTradeIntent(sr, userId, state, state.realized_pnl),
    verifyIntegrity: async (state) => {
      const integrity = await verifyIntegrity(sr, userId, state);
      if (!integrity.ok) throw new Error(`INTEGRITY_VIOLATION: ${integrity.reason}`);
    },
    projectPostSettlement: async (state) => projectPostSettlementMetadata(sr, userId, state),
  };
  const finalState = await runSettlementStages(
    event,
    handlers,
    (patch) => sr.entities.SettlementEvent.update(event.id, patch),
    beforeStage
  );

  await sr.entities.SettlementEvent.update(event.id, {
    status: 'completed',
    completed_at: nowIso(),
    integrity_verified: true,
    processing_owner: null,
    processing_started_at: null,
    error: null,
    next_retry_at: null,
  });

  return { symbol: event.symbol, side: event.side, quantity: event.quantity, price: event.price, realized_pnl: finalState.realized_pnl };
}

export default async function(req) {
  const base44 = createClientFromRequest(req);
  const user = await base44.auth.me();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sr = base44.asServiceRole;
  const userId = user.id;
  const workerId = `settlement-${crypto.randomUUID()}`;
  const now = Date.now();

  const processorLease = await acquireProcessorLease(sr, userId, workerId);
  if (!processorLease) {
    return Response.json({ ok: true, processed: 0, skipped: 'processor_already_running' });
  }

  // Keep the processor lease alive independently of event duration. A failed
  // heartbeat prevents the worker from claiming another event; ownership is
  // also verified at stage boundaries before further projection.
  let heartbeatFailure = null;
  let heartbeatInFlight = false;
  const heartbeatTimer = setInterval(async () => {
    if (heartbeatInFlight || heartbeatFailure) return;
    heartbeatInFlight = true;
    try {
      await renewProcessorLease(sr, processorLease);
    } catch (error) {
      heartbeatFailure = error;
    } finally {
      heartbeatInFlight = false;
    }
  }, 60_000);

  try {
    // Find pending, due retryable failures, or stale processing events.
    const allEvents = await sr.entities.SettlementEvent.filter({ user_id: userId });
    const processable = allEvents
      .filter((event) => isSettlementProcessable(event, now, STALE_LEASE_MS))
      .sort((a, b) => new Date(a.occurred_at) - new Date(b.occurred_at));

    const results = [];
    let settlementFailureSeen = false;
    for (const event of processable) {
      if (heartbeatFailure) throw new Error(`SETTLEMENT_LEASE_HEARTBEAT_FAILED: ${heartbeatFailure.message}`);
      await renewProcessorLease(sr, processorLease);
      if (!await ownsProcessorLease(sr, userId, processorLease)) {
        throw new Error('SETTLEMENT_LEASE_OWNERSHIP_LOST');
      }
      const claimResult = await claimEvent(sr, userId, event, workerId);
      if (!claimResult.claimed) continue;

      try {
        const result = await processEvent(sr, userId, claimResult.event, async () => {
          if (heartbeatFailure) throw new Error(`SETTLEMENT_LEASE_HEARTBEAT_FAILED: ${heartbeatFailure.message}`);
          if (!await ownsProcessorLease(sr, userId, processorLease)) {
            throw new Error('SETTLEMENT_LEASE_OWNERSHIP_LOST');
          }
        });
        results.push({ event_id: event.event_id, status: 'completed', ...result });
      } catch (error) {
        settlementFailureSeen = true;
        const failure = classifySettlementFailure(claimResult.event, error);
        await sr.entities.SettlementEvent.update(claimResult.event.id, failure);
        results.push({ event_id: event.event_id, status: failure.status, error: error.message });

        await audit(sr, userId, 'settlement_failed', 'error', {
          correlation_id: event.trade_intent_id,
          entity_type: 'SettlementEvent',
          entity_id: event.id,
          message: `Settlement failed for ${event.symbol} ${event.side} ${event.quantity}: ${error.message}`,
        });

        // Any failed projection may have mutated financial state before the
        // provider write or checkpoint failed. Block new exposure until replay
        // completes and an operator explicitly re-enables the session.
        try {
          await updateSessionState(sr, userId, SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED, `SETTLEMENT_RECOVERY_REQUIRED: ${error.message}`);
          await audit(sr, userId, 'financial_integrity_blocked', 'critical', {
            message: `Settlement recovery required: ${error.message}`,
          });
        } catch (e) { /* failure is already persisted on the event */ }
      }
    }

    const refreshedEvents = await sr.entities.SettlementEvent.filter({ user_id: userId });
    const unresolved = refreshedEvents.filter((event) => event.status !== 'completed');
    const recoveringBlockedSession = user.trading_session_state === SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED || settlementFailureSeen;
    if (recoveringBlockedSession && results.some((result) => result.status === 'completed') && unresolved.length === 0) {
      await sr.entities.User.update(userId, {
        financial_integrity_recovered_at: nowIso(),
        financial_integrity_manual_reenable_required: true,
        trading_active: false,
      });
      await audit(sr, userId, 'settlement_recovered_manual_reenable_required', 'warning', {
        message: 'All settlement events are complete and integrity verified; manual trading re-enable is required',
      });
    }

    const response = summarizeSettlementBatch(results, unresolved.length);
    return Response.json(response, { status: response.ok ? 200 : 409 });
  } finally {
    clearInterval(heartbeatTimer);
    await sr.entities.ScanLock.delete(processorLease.id).catch(() => {});
  }
}
