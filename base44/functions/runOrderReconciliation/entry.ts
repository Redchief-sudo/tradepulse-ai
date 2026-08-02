import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaOrder } from '../../shared/alpaca.ts';

// Continuous order reconciliation worker.
// Finds all nonterminal TradeIntents, fetches broker order state, ingests
// unrecorded fills, detects canceled/rejected/replaced orders, finalizes
// settlement, and alerts on stale orders.
//
// This runs frequently (every 5 minutes during market hours) via a scheduled
// workflow. Daily position reconciliation is a backstop — this is the primary
// order recovery mechanism.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const sr = base44.asServiceRole;
    const runTs = new Date().toISOString();

    // Find all nonterminal intents that have been submitted to a broker.
    const nonterminalStatuses = ['submitted', 'accepted', 'partially_filled'];
    const allIntents = await sr.entities.TradeIntent.filter({ user_id: user.id }, '-created_date', 200);
    const pending = allIntents.filter((i) =>
      nonterminalStatuses.includes(i.status) && i.broker_order_id && i.execution_mode !== 'internal_paper'
    );

    if (pending.length === 0) {
      return Response.json({ ok: true, message: 'No pending orders to reconcile', checked: 0 });
    }

    // Load broker credentials
    const creds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (!creds[0]) {
      return Response.json({ ok: true, message: 'No broker credentials — skipping', checked: 0 });
    }
    const cred = creds[0];
    const brokerCreds = { apiKey: cred.api_key, secretKey: cred.api_secret, mode: cred.mode };

    const results = [];
    const staleOrders = [];

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

        // Ingest any unrecorded incremental fills
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
          const existing = await sr.entities.Fill.filter({ user_id: user.id, fill_id: fillId });
          if (!existing || existing.length === 0) {
            await sr.entities.Fill.create({
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
            });
            newFills++;

            // Record audit event
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
            // Settle the filled portion
            await sr.entities.TradeIntent.update(intent.id, {
              status: 'settled',
              filled_quantity: cumulativeFilled,
              filled_avg_price: cumulativeAvgPrice,
              broker_terminal_status: status,
              unfilled_quantity: unfilledQty,
            });

            // Create Trade record if not exists
            const existingTrades = await sr.entities.Trade.filter({ user_id: user.id, client_order_id: intent.client_order_id });
            if (!existingTrades || existingTrades.length === 0) {
              let realizedPnl = null;
              if (intent.side === 'sell') {
                const lots = await sr.entities.PositionLot.filter({ user_id: user.id, symbol: intent.symbol });
                const closedLots = lots.filter((l) => {
                  const closureIds = JSON.parse(l.closure_fill_ids || '[]');
                  return closureIds.some((fid) => fid.includes(intent.broker_order_id));
                });
                realizedPnl = closedLots.reduce((s, l) => s + (l.realized_pnl || 0), 0);
              }

              await sr.entities.Trade.create({
                user_id: user.id,
                portfolio_id: intent.portfolio_id || null,
                symbol: intent.symbol,
                company_name: intent.company_name || intent.symbol,
                action: intent.side,
                shares: cumulativeFilled,
                price: cumulativeAvgPrice,
                total_value: cumulativeFilled * cumulativeAvgPrice,
                ai_recommended: intent.strategy_id !== 'manual',
                broker_order_id: intent.broker_order_id,
                client_order_id: intent.client_order_id,
                filled_qty: cumulativeFilled,
                filled_avg_price: cumulativeAvgPrice,
                order_status: isFailure ? 'partially_filled_then_canceled' : 'filled',
                source: intent.strategy_id,
                realized_pnl: realizedPnl,
              });
            }

            await sr.entities.AuditEvent.create({
              user_id: user.id,
              event_type: 'settlement_completed',
              severity: 'info',
              correlation_id: intent.trade_intent_id,
              entity_type: 'TradeIntent',
              entity_id: intent.id,
              message: `Reconciliation settled order ${intent.broker_order_id}: ${cumulativeFilled}/${intent.requested_quantity} filled, broker status: ${status}`,
              details: JSON.stringify({ filled_qty: cumulativeFilled, unfilled_qty: unfilledQty, broker_status: status }),
            });
          } else if (isFailure) {
            // No fill at all — mark as canceled/rejected
            await sr.entities.TradeIntent.update(intent.id, {
              status: status === 'rejected' ? 'rejected' : 'canceled',
              rejection_reason: `BROKER_${status.toUpperCase()}`,
              broker_terminal_status: status,
              unfilled_quantity: intent.requested_quantity,
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
          }

          results.push({ intent_id: intent.trade_intent_id, symbol: intent.symbol, status: 'settled', broker_status: status, new_fills: newFills, filled_qty: cumulativeFilled });
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
      } catch (e) {}
    }

    return Response.json({
      ok: true,
      run_timestamp: runTs,
      checked: pending.length,
      settled: results.filter((r) => r.status === 'settled').length,
      still_pending: results.filter((r) => r.status === 'still_pending').length,
      stale_orders: staleOrders.length,
      errors: results.filter((r) => r.status === 'error').length,
      results,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}