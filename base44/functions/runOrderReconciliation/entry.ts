import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaActivities, getAlpacaOrder, normalizeAlpacaActivitySide, normalizeAlpacaSymbol, inferAlpacaAssetClass } from '../../shared/alpaca.ts';
import { recordFill } from '../../shared/execution.ts';
import { nowIso } from '../../shared/lotAccounting.ts';

// Continuous order reconciliation worker.
// Finds all nonterminal TradeIntents, fetches broker order state, ingests
// unrecorded fills, detects canceled/rejected/replaced orders, finalizes
// settlement, and alerts on stale orders.
//
// ARCHITECTURE: This function does NOT reimplement fill ingestion or
// settlement accounting. It retrieves broker state and calls the SAME
// canonical execution primitives used by the live execution gateway:
//   - recordFill() for user-scoped, dedup-safe fill ingestion
//   - SettlementEvent creation for canonical, resumable projection
//   - processSettlementQueue as the sole financial-state writer
//
// This ensures financial accounting has exactly one implementation.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const sr = base44.asServiceRole;
    const runTs = new Date().toISOString();
    const workerId = `recon-${crypto.randomUUID()}`;

    // Find all nonterminal intents that have been submitted to a broker.
    const nonterminalStatuses = ['submitted', 'accepted', 'partially_filled'];
    const allIntents = await sr.entities.TradeIntent.filter({ user_id: user.id }, '-created_date', 200);
    const pending = allIntents.filter((i) =>
      nonterminalStatuses.includes(i.status) && i.broker_order_id && i.execution_mode !== 'internal_paper'
    );

    // Load broker credentials
    const creds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (!creds[0]) {
      return Response.json({ ok: true, message: 'No broker credentials — skipping', checked: 0 });
    }
    const cred = creds[0];
    const brokerCreds = { apiKey: cred.api_key, secretKey: cred.api_secret, mode: cred.mode };

    const results = [];
    const staleOrders = [];

    // === EXTERNAL BROKER ACTIVITY INGESTION ===
    // Manual Alpaca transactions have no pre-existing TradeIntent. Preserve
    // their broker identity in an explicit broker_external intent, then route
    // the authoritative activity through the same Fill -> SettlementEvent
    // boundary as application-originated orders. Never infer missing fields.
    let activities = [];
    try {
      activities = await getAlpacaActivities({
        ...brokerCreds,
        sinceDate: new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10),
      });
    } catch (error) {
      await sr.entities.AuditEvent.create({
        user_id: user.id,
        event_type: 'broker_activity_ingestion_failed',
        severity: 'error',
        entity_type: 'Fill',
        message: `Broker activity ingestion failed: ${error.message}`,
        details: JSON.stringify({ error: error.message }),
      });
      results.push({ status: 'external_activity_error', error: error.message });
    }
    for (const activity of activities) {
      const activityId = activity.id || activity.activity_id;
      const assetClass = inferAlpacaAssetClass(activity.symbol, activity.asset_class);
      const symbol = normalizeAlpacaSymbol(activity.symbol, assetClass);
      const side = normalizeAlpacaActivitySide(activity.side);
      const quantity = Number(activity.qty);
      const price = Number(activity.price);
      const occurredAt = activity.transaction_time || activity.date;
      if (!activityId || !symbol || !['buy', 'sell'].includes(side) || quantity <= 0 || price <= 0 || !occurredAt) {
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: 'broker_activity_rejected',
          severity: 'warning',
          entity_type: 'Fill',
          entity_id: activityId || null,
          message: 'Broker activity missing stable identity or required execution fields',
          details: JSON.stringify({ activity_id: activityId || null, symbol, side, quantity, price, occurred_at: occurredAt || null }),
        });
        continue;
      }

      const fillId = `alpaca-activity:${activityId}`;
      const existingFills = await sr.entities.Fill.filter({ user_id: user.id, fill_id: fillId });
      if (existingFills.length > 0) continue;

      // Activities already associated with an application intent are ingested
      // by the order loop below and must not be reclassified as external.
      const mappedIntent = allIntents.find((intent) =>
        (activity.order_id && intent.broker_order_id === activity.order_id) ||
        (activity.order_client_id && intent.client_order_id === activity.order_client_id)
      );
      if (mappedIntent) continue;

      const tradeIntentId = `broker-external:${activityId}`;
      const clientOrderId = activity.order_client_id || `broker-external-${activityId}`;
      const existingExternalIntents = await sr.entities.TradeIntent.filter({ user_id: user.id, trade_intent_id: tradeIntentId });
      const externalIntent = existingExternalIntents[0] || await sr.entities.TradeIntent.create({
        user_id: user.id,
        trade_intent_id: tradeIntentId,
        idempotency_key: tradeIntentId,
        strategy_id: 'broker_external',
        symbol,
        side,
        requested_quantity: quantity,
        execution_mode: cred.mode === 'live' ? 'live' : 'broker_paper',
        status: 'filled',
        settlement_state: 'pending',
        broker: 'alpaca',
        broker_order_id: activity.order_id || null,
        client_order_id: clientOrderId,
        filled_quantity: quantity,
        filled_avg_price: price,
        broker_terminal_status: 'filled',
        unfilled_quantity: 0,
        company_name: symbol,
        asset_class: assetClass,
      });
      const executionMode = externalIntent.execution_mode;
      const inserted = await recordFill(sr, {
        user_id: user.id,
        fill_id: fillId,
        broker_order_id: activity.order_id || null,
        client_order_id: clientOrderId,
        trade_intent_id: tradeIntentId,
        symbol,
        asset_id: symbol,
        side,
        filled_quantity: quantity,
        filled_price: price,
        notional: quantity * price,
        commission: 0,
        fees: 0,
        timestamp: new Date(occurredAt).toISOString(),
        venue: cred.mode === 'live' ? 'alpaca' : 'alpaca_paper',
        strategy_id: 'broker_external',
        execution_mode: executionMode,
        asset_class: assetClass,
      });
      if (inserted) {
        await sr.entities.SettlementEvent.create({
          user_id: user.id,
          event_id: fillId,
          trade_intent_id: tradeIntentId,
          broker_order_id: activity.order_id || null,
          broker_fill_id: String(activityId),
          client_order_id: clientOrderId,
          symbol,
          side,
          quantity,
          price,
          notional: quantity * price,
          occurred_at: new Date(occurredAt).toISOString(),
          status: 'pending',
          execution_mode: executionMode,
          asset_class: assetClass,
          company_name: symbol,
          strategy_id: 'broker_external',
          venue: cred.mode === 'live' ? 'alpaca' : 'alpaca_paper',
        });
        results.push({ intent_id: tradeIntentId, symbol, status: 'external_activity_ingested', fill_id: fillId });
      }
    }

    // === STALE CLAIM RECOVERY ===
    // Find intents stuck in 'claimed' state with stale claims (> 5 min).
    // These are workers that crashed mid-settlement. Re-claim and recover.
    const allClaimed = await sr.entities.TradeIntent.filter({ user_id: user.id, settlement_state: 'claimed' }, '-created_date', 50);
    const staleClaimed = allClaimed.filter((i) => {
      if (!i.settlement_claimed_at) return true;
      return Date.now() - new Date(i.settlement_claimed_at).getTime() > 5 * 60 * 1000;
    });

    for (const intent of staleClaimed) {
      // Reset the settlement state so the settlement processor can retry.
      // In the new architecture, the processor handles claiming via
      // SettlementEvent status, not TradeIntent settlement_state.
      if (intent.filled_quantity > 0) {
        // Create a SettlementEvent for the fills if one doesn't exist yet
        const existingEvents = await sr.entities.SettlementEvent.filter({ user_id: user.id, trade_intent_id: intent.trade_intent_id });
        if (existingEvents.length === 0) {
          await sr.entities.SettlementEvent.create({
            user_id: user.id,
            portfolio_id: intent.portfolio_id || null,
            event_id: `${intent.broker_order_id || intent.client_order_id}:recovery`,
            trade_intent_id: intent.trade_intent_id,
            broker_order_id: intent.broker_order_id,
            client_order_id: intent.client_order_id,
            symbol: intent.symbol, side: intent.side,
            quantity: intent.filled_quantity, price: intent.filled_avg_price,
            notional: intent.filled_quantity * intent.filled_avg_price,
            occurred_at: intent.accepted_at || intent.created_date,
            status: 'pending',
            execution_mode: intent.execution_mode,
            asset_class: intent.asset_class || 'stocks',
            company_name: intent.company_name || intent.symbol,
            sector: intent.sector || '',
            strategy_id: intent.strategy_id,
            decision_id: intent.decision_id,
            regime: intent.regime || null,
          });
        }
        await sr.entities.TradeIntent.update(intent.id, { settlement_state: 'pending' });
        results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'recovery_event_created' });
      } else {
        await sr.entities.TradeIntent.update(intent.id, { status: 'canceled', settlement_state: 'settled', rejection_reason: 'CRASH_RECOVERY_NO_FILLS' });
        results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'recovered_canceled', reason: 'no_fills' });
      }
    }

    // === PENDING ORDER RECONCILIATION ===
    for (const intent of pending) {
      const alpacaMode = intent.execution_mode === 'live' ? 'live' : 'paper';
      const orderCreds = { ...brokerCreds, mode: alpacaMode };

      let order;
      try {
        const t0 = Date.now();
        order = await getAlpacaOrder(orderCreds, intent.broker_order_id);
        const latencyMs = Date.now() - t0;

        const status = order.status;
        const cumulativeFilled = Number(order.filled_qty) || 0;
        const cumulativeAvgPrice = Number(order.filled_avg_price) || 0;

        // Ingest any unrecorded incremental fills using the CANONICAL recordFill.
        let lastFilledQty = intent.filled_quantity || 0;
        let lastFilledPrice = intent.filled_avg_price || 0;
        let newFills = 0;

        if (cumulativeFilled > lastFilledQty) {
          const incrementalQty = cumulativeFilled - lastFilledQty;
          const prevNotional = lastFilledQty * (lastFilledPrice || 0);
          const currNotional = cumulativeFilled * cumulativeAvgPrice;
          const incrementalNotional = currNotional - prevNotional;
          const incrementalPrice = incrementalQty > 0 && incrementalNotional > 0
            ? incrementalNotional / incrementalQty
            : (cumulativeAvgPrice > 0 ? cumulativeAvgPrice : Number(intent.limit_price || 0));

          const fillId = `${intent.broker_order_id}:fill:${cumulativeFilled}`;

          // Use canonical recordFill — user-scoped, dedup-safe
          const inserted = await recordFill(sr, {
            user_id: user.id,
            portfolio_id: intent.portfolio_id || null,
            fill_id: fillId,
            broker_order_id: intent.broker_order_id,
            client_order_id: intent.client_order_id,
            trade_intent_id: intent.trade_intent_id,
            symbol: intent.symbol,
            asset_id: intent.native_asset_id || intent.symbol,
            side: intent.side,
            filled_quantity: incrementalQty,
            filled_price: incrementalPrice,
            notional: incrementalQty * incrementalPrice,
            commission: 0,
            fees: 0,
            slippage: 0,
            timestamp: new Date().toISOString(),
            venue: alpacaMode === 'live' ? 'alpaca' : 'alpaca_paper',
            strategy_id: intent.strategy_id,
            decision_id: intent.decision_id,
            execution_mode: intent.execution_mode,
            asset_class: intent.asset_class || 'stocks',
            arrival_price: Number(intent.limit_price) || null,
            decision_price: Number(intent.limit_price) || null,
            submission_price: Number(intent.limit_price) || null,
            first_fill_price: lastFilledQty === 0 ? incrementalPrice : null,
            vwap_fill_price: cumulativeAvgPrice > 0 ? cumulativeAvgPrice : null,
            implementation_shortfall: Number(intent.limit_price) && incrementalPrice ? Number(intent.limit_price) - incrementalPrice : null,
            fill_latency_ms: null,
          });

          if (inserted) {
            newFills++;
            // Create a SettlementEvent — the settlement processor handles all projection.
            await sr.entities.SettlementEvent.create({
              user_id: user.id,
              portfolio_id: intent.portfolio_id || null,
              event_id: fillId,
              trade_intent_id: intent.trade_intent_id,
              broker_order_id: intent.broker_order_id,
              broker_fill_id: fillId,
              client_order_id: intent.client_order_id,
              symbol: intent.symbol, side: intent.side,
              quantity: incrementalQty, price: incrementalPrice,
              notional: incrementalQty * incrementalPrice,
              occurred_at: nowIso(),
              status: 'pending',
              execution_mode: intent.execution_mode,
              asset_class: intent.asset_class || 'stocks',
              company_name: intent.company_name || intent.symbol,
              sector: intent.sector || '',
              strategy_id: intent.strategy_id,
              decision_id: intent.decision_id,
              regime: intent.regime || null,
              venue: alpacaMode === 'live' ? 'alpaca' : 'alpaca_paper',
            });

            await sr.entities.AuditEvent.create({
              user_id: user.id,
              event_type: 'fill_recorded',
              severity: 'info',
              correlation_id: intent.trade_intent_id,
              entity_type: 'Fill',
              entity_id: fillId,
              message: `Reconciliation ingested fill: ${incrementalQty} ${intent.symbol} @ $${incrementalPrice.toFixed(2)}`,
              details: JSON.stringify({ intent_id: intent.trade_intent_id, fill_id: fillId, qty: incrementalQty, price: incrementalPrice }),
              broker_api_latency_ms: latencyMs,
            });
          }
        }

        // Check for terminal states
        const isTerminal = ['filled', 'done_for_day', 'canceled', 'rejected', 'expired', 'replaced'].includes(status);

        if (isTerminal) {
          const unfilledQty = Math.max(0, intent.requested_quantity - cumulativeFilled);
          const isFailure = ['canceled', 'rejected', 'expired', 'replaced'].includes(status);

          if (cumulativeFilled > 0) {
            // Update terminal status — the settlement processor handles projection
            await sr.entities.TradeIntent.update(intent.id, {
              status: cumulativeFilled >= intent.requested_quantity ? 'filled' : 'partially_filled',
              filled_quantity: cumulativeFilled,
              filled_avg_price: cumulativeAvgPrice,
              broker_terminal_status: status,
              unfilled_quantity: unfilledQty,
              released_cash: isFailure ? Math.max(0, (intent.reserved_cash || 0) - (intent.consumed_cash || 0)) : 0,
              reservation_status: isFailure ? 'released' : (cumulativeFilled >= intent.requested_quantity ? 'consumed' : 'partially_consumed'),
            });

            // Invoke the settlement processor to project all pending events.
            try { await base44.functions.invoke('processSettlementQueue', {}); }
            catch (e) { /* non-fatal — scheduled processor will retry */ }

            results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'settled', broker_status: status, new_fills: newFills, filled_qty: cumulativeFilled });
          } else if (isFailure) {
            // No fill at all — mark as canceled/rejected and release reservation
            await sr.entities.TradeIntent.update(intent.id, {
              status: status === 'rejected' ? 'rejected' : 'canceled',
              settlement_state: 'settled',
              rejection_reason: `BROKER_${status.toUpperCase()}`,
              broker_terminal_status: status,
              unfilled_quantity: intent.requested_quantity,
              released_cash: intent.reserved_cash || 0,
              reserved_cash: 0,
              reservation_status: 'released',
            });

            await sr.entities.AuditEvent.create({
              user_id: user.id,
              event_type: 'order_canceled',
              severity: 'warning',
              correlation_id: intent.trade_intent_id,
              entity_type: 'TradeIntent',
              entity_id: intent.id,
              message: `Reconciliation detected ${status} order ${intent.broker_order_id} with zero fills`,
              details: JSON.stringify({ broker_status: status, requested_qty: intent.requested_quantity }),
            });

            results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'canceled', broker_status: status, new_fills: 0 });
          }
        } else {
          // Still pending — check for staleness
          const ageMinutes = (Date.now() - new Date(intent.created_date).getTime()) / 60000;
          if (ageMinutes > 5) {
            staleOrders.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, age_minutes: Math.round(ageMinutes), broker_status: status });

            await sr.entities.AuditEvent.create({
              user_id: user.id,
              event_type: 'stale_order',
              severity: 'warning',
              correlation_id: intent.trade_intent_id,
              entity_type: 'TradeIntent',
              entity_id: intent.id,
              message: `Stale order: ${intent.symbol} open for ${Math.round(ageMinutes)}m (broker status: ${status})`,
              details: JSON.stringify({ age_minutes: Math.round(ageMinutes), broker_status: status, broker_order_id: intent.broker_order_id }),
            });
          }

          // Update cumulative state
          if (cumulativeFilled !== (intent.filled_quantity || 0)) {
            await sr.entities.TradeIntent.update(intent.id, {
              status: cumulativeFilled > 0 ? 'partially_filled' : 'accepted',
              filled_quantity: cumulativeFilled,
              filled_avg_price: cumulativeAvgPrice,
            });
          }

          results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'still_pending', broker_status: status, new_fills: newFills, age_minutes: Math.round(ageMinutes) });
        }
      } catch (e) {
        // Broker API error — record but don't crash
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: 'broker_outage',
          severity: 'error',
          correlation_id: intent.trade_intent_id,
          entity_type: 'TradeIntent',
          entity_id: intent.id,
          message: `Reconciliation broker error for ${intent.symbol}: ${e.message}`,
          details: JSON.stringify({ error: e.message, broker_order_id: intent.broker_order_id }),
        });
        results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'error', error: e.message });
      }
    }

    // Alert on stale orders
    if (staleOrders.length > 0) {
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse: ${staleOrders.length} stale order(s) detected`,
          body: staleOrders.map((s) => `${s.symbol}: open for ${s.age_minutes}m (broker: ${s.broker_status})`).join('\n'),
        });
      } catch (e) {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', entity_type: 'TradeIntent', message: `Stale order email failed: ${e.message}` });
      }
    }

    let positionReconciliation = null;
    try {
      const invoked = await base44.functions.invoke('syncBrokerPositions', {});
      positionReconciliation = invoked?.data || invoked;
      if (!positionReconciliation || positionReconciliation.ok === false) {
        results.push({ status: 'position_reconciliation_error', error: positionReconciliation?.error || `Position drift: ${(positionReconciliation?.blocked || []).join(',')}` });
      }
    } catch (error) {
      results.push({ status: 'position_reconciliation_error', error: error.message });
      try {
        await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'position_reconciliation_invocation_failed', severity: 'error', message: error.message });
      } catch (auditError) { /* response remains explicit */ }
    }

    const reconciliationErrors = results.filter((result) => ['error', 'external_activity_error', 'position_reconciliation_error'].includes(result.status));
    const response = {
      ok: reconciliationErrors.length === 0,
      run_timestamp: runTs,
      checked: pending.length,
      settled: results.filter((r) => r.status === 'settled').length,
      canceled: results.filter((r) => r.status === 'canceled').length,
      still_pending: results.filter((r) => r.status === 'still_pending').length,
      already_claimed: results.filter((r) => r.status === 'already_claimed').length,
      stale_orders: staleOrders.length,
      errors: reconciliationErrors.length,
      position_reconciliation: positionReconciliation,
      results,
    };
    return Response.json(response, { status: response.ok ? 200 : 502 });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
