// Canonical execution + accounting boundary.
// Every trading surface (autonomous cycle, stop-loss, dashboard exit, AI assistant,
// manual UI) funnels through settleTrade(). Accounting is mutated ONLY from a
// confirmed broker fill (or an explicit paper fill when no broker is connected).
// A rejected / canceled / pending broker order NEVER mutates the ledger.

import { placeAlpacaOrder, getAlpacaOrder } from './alpaca.ts';

const FILL_TIMEOUT_MS = 15000;
const POLL_INTERVAL_MS = 1000;

function isTerminalFailure(status) {
  return ['rejected', 'canceled', 'expired', 'replaced'].includes(status);
}

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
  // Timeout — treat as pending; do NOT settle.
  const order = await getAlpacaOrder(creds, orderId);
  return { status: order.status || 'pending', filled_qty: Number(order.filled_qty) || 0, filled_avg_price: Number(order.filled_avg_price) || 0 };
}

// settleTrade(base44, user, intent) -> { status, ... }
// status: 'filled' | 'paper_filled' | 'rejected' | 'pending' | 'invalid'
// intent: { symbol, action, qty, price (reference/paper fill), source, company_name,
//           sector, confidence, target_price, stop_loss, ai_recommended, notes,
//           ml_score, technical_score, momentum_score, risk_score, reasoning,
//           asset_class, recordDecision }
export async function settleTrade(base44, user, intent) {
  const sr = base44.asServiceRole;
  const symbol = String(intent.symbol || '').toUpperCase();
  const action = intent.action;
  const qty = Number(intent.qty);
  if (!symbol || (action !== 'buy' && action !== 'sell') || !qty || qty <= 0) {
    return { status: 'invalid', error: 'symbol, action, and positive qty are required' };
  }

  const clientOrderId = `tp-${user.id || 'anon'}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const brokerConnected = user.broker === 'alpaca' && user.broker_api_key && user.broker_api_secret;
  const mode = user.broker_mode || 'paper';

  let brokerOrderId = null;
  let orderStatus = 'paper_filled';
  let filledQty = qty;
  let filledPrice = Number(intent.price) || 0;

  if (brokerConnected) {
    const creds = { apiKey: user.broker_api_key, secretKey: user.broker_api_secret, mode };
    let placed;
    try {
      placed = await placeAlpacaOrder({ ...creds, symbol, qty, side: action, client_order_id: clientOrderId });
    } catch (e) {
      return { status: 'rejected', error: e.message, symbol, action, qty, clientOrderId };
    }
    brokerOrderId = placed.id;
    const result = await pollAlpacaFill(creds, brokerOrderId);
    orderStatus = result.status;

    if (result.filled_qty > 0) {
      filledQty = result.filled_qty;
      filledPrice = result.filled_avg_price > 0 ? result.filled_avg_price : Number(intent.price);
    } else if (isTerminalFailure(orderStatus)) {
      // Broker rejected/canceled the order — NO accounting mutation.
      return { status: 'rejected', orderStatus, brokerOrderId, symbol, action, qty, clientOrderId };
    } else {
      // Pending / timeout — order may still fill later; do not settle now.
      return { status: 'pending', orderStatus, brokerOrderId, symbol, action, qty, clientOrderId };
    }
  }

  if (!filledPrice || filledPrice <= 0) {
    return { status: 'invalid', error: 'no fill price available', symbol, action, qty };
  }

  const totalValue = filledQty * filledPrice;

  // Atomic accounting settlement from the confirmed fill.
  const trade = await sr.entities.Trade.create({
    symbol,
    company_name: intent.company_name || symbol,
    action,
    shares: filledQty,
    price: filledPrice,
    total_value: totalValue,
    notes: intent.notes,
    ai_recommended: !!intent.ai_recommended,
    broker_order_id: brokerOrderId,
    client_order_id: clientOrderId,
    filled_qty: filledQty,
    filled_avg_price: filledPrice,
    order_status: orderStatus,
    source: intent.source || 'manual',
  });

  const holdings = await sr.entities.Holding.list();
  const existing = holdings.find((h) => String(h.symbol).toUpperCase() === symbol);

  if (action === 'buy') {
    if (existing) {
      const ts = existing.shares + filledQty;
      const tc = existing.shares * existing.avg_price + totalValue;
      await sr.entities.Holding.update(existing.id, {
        shares: ts,
        avg_price: tc / ts,
        current_price: filledPrice,
        stop_loss: intent.stop_loss ?? existing.stop_loss,
        target_price: intent.target_price ?? existing.target_price,
      });
    } else {
      await sr.entities.Holding.create({
        symbol,
        company_name: intent.company_name || symbol,
        shares: filledQty,
        avg_price: filledPrice,
        current_price: filledPrice,
        sector: intent.sector || '',
        day_change_percent: 0,
        stop_loss: intent.stop_loss,
        target_price: intent.target_price,
      });
    }
  } else {
    if (existing) {
      const newShares = existing.shares - filledQty;
      if (newShares <= 0) {
        await sr.entities.Holding.delete(existing.id);
      } else {
        await sr.entities.Holding.update(existing.id, { shares: newShares, current_price: filledPrice });
      }
    }
    // Sell with no existing holding is a broker-only event (shouldn't happen post-validation);
    // the Trade ledger still records it for audit.
  }

  let decisionId = null;
  if (intent.recordDecision) {
    const d = await sr.entities.AITradeDecision.create({
      symbol,
      company_name: intent.company_name || symbol,
      sector: intent.sector || '',
      asset_class: intent.asset_class || 'stocks',
      action,
      shares: filledQty,
      price: filledPrice,
      position_value: totalValue,
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
    decisionId = d.id;
  }

  return {
    status: brokerConnected ? 'filled' : 'paper_filled',
    orderStatus,
    brokerOrderId,
    clientOrderId,
    filled_qty: filledQty,
    filled_avg_price: filledPrice,
    total_value: totalValue,
    trade_id: trade.id,
    decision_id: decisionId,
    symbol,
    action,
  };
}