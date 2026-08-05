import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaActivities } from '../../shared/alpaca.ts';

// Broker-authoritative reconciliation. The broker is the source of truth for positions.
// USER-SCOPED: all queries filter by user_id. Credentials read from BrokerCredential.
//
// LOT-CONSISTENT: every broker-derived Holding is backed by a PositionLot so the
// lot ledger (the source of truth) and the holding projection never diverge.
// Without this, a later sell fill would see zero available lots and fail with
// SELL_FILL_EXCEEDS_ACCOUNTED_POSITION.
//
// FAIL-CLOSED: when a position disappears at the broker but no authoritative exit
// fill can be identified, the holding is NOT deleted and the lots are NOT closed.
// The position is flagged for review — the accounting lifecycle is never finalized
// without closure evidence. This prevents stale lots from resurrecting a closed
// position and prevents wrong P&L attribution from a guessed exit price.

function nowIso() { return new Date().toISOString(); }
function genId(prefix) { return `${prefix}-${crypto.randomUUID()}`; }

// Close open lots FIFO for a symbol at the given exit price. Returns realized P&L.
// If no lots exist but a holding does (legacy pre-lot state), backfills a lot from
// the holding first so the close can proceed.
async function closeLotsFifo(sr, userId, sym, exitQty, exitPrice, holding) {
  let lots = await sr.entities.PositionLot.filter({ user_id: userId, symbol: sym });
  let openLots = lots
    .filter((l) => l.status === 'open' || l.status === 'partially_closed')
    .sort((a, b) => new Date(a.acquisition_timestamp) - new Date(b.acquisition_timestamp));

  // Backfill: if there are no open lots but a holding exists, create a lot from
  // the holding so the ledger is consistent before closing.
  if (openLots.length === 0 && holding && holding.shares > 0) {
    const lot = await sr.entities.PositionLot.create({
      user_id: userId,
      lot_id: genId('lot'),
      symbol: sym,
      company_name: holding.company_name || sym,
      sector: holding.sector || '',
      asset_class: holding.asset_class || 'stocks',
      originating_fill_id: null,
      quantity_opened: holding.shares,
      quantity_remaining: holding.shares,
      acquisition_price: holding.avg_price,
      acquisition_timestamp: holding.created_date || nowIso(),
      status: 'open',
      realized_pnl: 0,
      closure_fill_ids: '[]',
      cost_basis_method: 'fifo',
      provenance_source: 'broker_import',
      provenance_quality: 'ambiguous',
    });
    openLots = [lot];
  }

  const availableQty = openLots.reduce((s, l) => s + l.quantity_remaining, 0);
  if (exitQty > availableQty + 0.0001) {
    throw new Error(`RECONCILIATION_CLOSE_EXCEEDS_LOTS: ${sym} exit ${exitQty} > available ${availableQty}`);
  }

  let remaining = exitQty;
  let realizedPnl = 0;
  for (const lot of openLots) {
    if (remaining <= 0.0001) break;
    const closeQty = Math.min(lot.quantity_remaining, remaining);
    const lotPnl = (exitPrice - lot.acquisition_price) * closeQty;
    realizedPnl += lotPnl;
    const newRemaining = lot.quantity_remaining - closeQty;
    await sr.entities.PositionLot.update(lot.id, {
      quantity_remaining: newRemaining,
      status: newRemaining <= 0.0001 ? 'closed' : 'partially_closed',
      realized_pnl: (lot.realized_pnl || 0) + lotPnl,
    });
    remaining -= closeQty;
  }
  return realizedPnl;
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const runTs = nowIso();

    // Read credentials from the secure BrokerCredential entity
    const creds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (!creds[0] || creds[0].broker !== 'alpaca') {
      return Response.json({ error: 'Alpaca not connected — nothing to reconcile' }, { status: 400 });
    }
    const cred = creds[0];
    const base = cred.mode === 'live' ? 'https://api.alpaca.markets/v2' : 'https://paper-api.alpaca.markets/v2';
    const hdrs = { 'APCA-API-KEY-ID': cred.api_key, 'APCA-API-SECRET-KEY': cred.api_secret };

    let brokerPositions = [];
    try {
      const res = await fetch(`${base}/positions`, { headers: hdrs });
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: `Alpaca HTTP ${res.status}: ${txt}`, action_taken: 'flagged_for_review' });
        return Response.json({ error: `Alpaca HTTP ${res.status}: ${txt}` }, { status: 502 });
      }
      brokerPositions = await res.json();
    } catch (e) {
      await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, event_type: 'broker_unreachable', symbol: '*', details: e.message, action_taken: 'flagged_for_review' });
      return Response.json({ error: e.message }, { status: 502 });
    }

    const brokerMap = {};
    brokerPositions.forEach((p) => {
      brokerMap[String(p.symbol).toUpperCase()] = {
        symbol: p.symbol, shares: Number(p.qty), avg_price: Number(p.avg_entry_price),
        current_price: Number(p.current_price), market_value: Number(p.market_value),
      };
    });

    // USER-SCOPED: only this user's holdings
    const appHoldings = await sr.entities.Holding.filter({ user_id: user.id });
    const appMap = {};
    appHoldings.forEach((h) => { appMap[String(h.symbol).toUpperCase()] = h; });

    const events = [];
    const created = [], updated = [], removed = [], blocked = [];

    // broker -> app — create lot + holding for new broker positions, adjust on drift.
    for (const [sym, bp] of Object.entries(brokerMap)) {
      const existing = appMap[sym];
      if (!existing) {
        // Create a PositionLot for the broker-derived position so the lot ledger
        // stays consistent with the holding projection. Without this, a later
        // sell fill would see zero available lots.
        await sr.entities.PositionLot.create({
          user_id: user.id,
          lot_id: genId('lot'),
          symbol: bp.symbol,
          company_name: bp.symbol,
          sector: '',
          asset_class: 'stocks',
          originating_fill_id: null,
          quantity_opened: bp.shares,
          quantity_remaining: bp.shares,
          acquisition_price: bp.avg_price,
          acquisition_timestamp: nowIso(),
          status: 'open',
          realized_pnl: 0,
          closure_fill_ids: '[]',
          cost_basis_method: 'fifo',
          provenance_source: 'broker_import',
          provenance_quality: 'unverified',
        });
        await sr.entities.Holding.create({
          user_id: user.id, symbol: bp.symbol, company_name: bp.symbol, shares: bp.shares, avg_price: bp.avg_price,
          current_price: bp.current_price, sector: '', day_change_percent: 0,
        });
        created.push(sym);
        events.push({ event_type: 'new_from_broker', symbol: sym, broker_qty: bp.shares, broker_avg_price: bp.avg_price, action_taken: 'created_lot_and_holding' });
      } else {
        const qtyDrift = Math.abs((existing.shares || 0) - bp.shares) > 0.0001;
        const priceDrift = existing.current_price && Math.abs(existing.current_price - bp.current_price) > 0.01;
        if (qtyDrift) {
          // Reconcile the lot ledger to match the broker-authoritative quantity.
          const diff = bp.shares - (existing.shares || 0);
          if (diff > 0) {
            // Broker has more than app — open a reconciliation lot for the difference.
            await sr.entities.PositionLot.create({
              user_id: user.id,
              lot_id: genId('lot'),
              symbol: bp.symbol,
              company_name: existing.company_name || bp.symbol,
              sector: existing.sector || '',
              asset_class: existing.asset_class || 'stocks',
              originating_fill_id: null,
              quantity_opened: diff,
              quantity_remaining: diff,
              acquisition_price: bp.avg_price,
              acquisition_timestamp: nowIso(),
              status: 'open',
              realized_pnl: 0,
              closure_fill_ids: '[]',
              cost_basis_method: 'fifo',
              provenance_source: 'broker_import',
              provenance_quality: 'unverified',
            });
          } else if (diff < 0) {
            // Broker has less than app — locate the ACTUAL broker sell fill.
            // AUTHORITATIVE MATCH ONLY: auto-settle only when the fill can be
            // bound to a broker order ID or client order ID from the app's Trade
            // records for this symbol. Quantity-only matches are ambiguous
            // (multiple sells of the same size on different days) and produce
            // a REVIEW CANDIDATE, never automatic P&L settlement.
            // (Fixes Rev.9 defect #6: partial reconciliation used quantity-only
            // matching, which can select the wrong transaction for frequently-
            // traded symbols. Also preserves the activity-fetch error reason
            // instead of silently swallowing it.)
            let partialExitPrice = null;
            let matchMethod = null;
            let activityError = null;
            try {
              const sinceDate = new Date(existing.created_date || Date.now() - 30 * 86400000).toISOString().slice(0, 10);
              const activities = await getAlpacaActivities({ apiKey: cred.api_key, secretKey: cred.api_secret, mode: cred.mode, sinceDate });
              const sellFills = activities.filter((a) =>
                String(a.symbol).toUpperCase() === sym && a.side === 'sell'
              );
              // Look up the app's Trade records for sells of this symbol to
              // find known broker order IDs and client order IDs.
              const symbolTrades = await sr.entities.Trade.filter({ user_id: user.id, symbol: sym, action: 'sell' });
              const tradeOrderIds = symbolTrades.map((t) => t.broker_order_id).filter(Boolean);
              const tradeClientOrderIds = symbolTrades.map((t) => t.client_order_id).filter(Boolean);
              // Tier 1: match by broker order ID or client order ID — authoritative.
              const idMatch = sellFills.find((a) =>
                (a.order_id && tradeOrderIds.includes(a.order_id)) ||
                (a.order_client_id && tradeClientOrderIds.includes(a.order_client_id))
              );
              if (idMatch && Math.abs(Number(idMatch.qty) - Math.abs(diff)) < 0.0001) {
                partialExitPrice = Number(idMatch.price);
                matchMethod = 'order_id';
              }
              // Tier 2: quantity-only match — NOT authoritative. Do NOT auto-settle.
              // The fill cannot be proven to correspond to this position transition.
            } catch (e) {
              activityError = { message: e.message, timestamp: nowIso() };
            }

            if (partialExitPrice && partialExitPrice > 0 && matchMethod === 'order_id') {
              try {
                await closeLotsFifo(sr, user.id, sym, Math.abs(diff), partialExitPrice, existing);
              } catch (e) {
                await sr.entities.Holding.update(existing.id, { reconciliation_blocked: true, reconciliation_blocked_reason: 'LOT_CLOSE_FAILED', reconciliation_blocked_at: nowIso() });
                blocked.push(sym);
                events.push({ event_type: 'reconciliation_adjustment', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, action_taken: 'flagged_for_review_lot_close_failed', details: e.message });
                continue;
              }
            } else {
              // No authoritative fill found — flag for review, don't fabricate P&L.
              // Preserve the reason: activity error vs no ID match vs quantity-only.
              await sr.entities.Holding.update(existing.id, { reconciliation_blocked: true, reconciliation_blocked_reason: 'NO_AUTHORITATIVE_FILL', reconciliation_blocked_at: nowIso() });
              blocked.push(sym);
              const reason = activityError
                ? `Activities unreachable: ${activityError.message} (at ${activityError.timestamp})`
                : `Partial qty decrease of ${Math.abs(diff)}: no broker order ID match from Trade records. Quantity-only match is ambiguous — flagged for manual review.`;
              events.push({ event_type: 'reconciliation_adjustment', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, action_taken: 'flagged_for_review_no_authoritative_fill', details: reason });
              continue;
            }
          }
          await sr.entities.Holding.update(existing.id, { shares: bp.shares, avg_price: bp.avg_price, current_price: bp.current_price });
          updated.push({ symbol: sym, from: existing.shares, to: bp.shares });
          events.push({ event_type: 'qty_drift', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, app_avg_price: existing.avg_price, broker_avg_price: bp.avg_price, action_taken: 'reconciled_lots_and_holding' });
        } else if (priceDrift) {
          await sr.entities.Holding.update(existing.id, { current_price: bp.current_price });
          events.push({ event_type: 'price_drift', symbol: sym, app_current_price: existing.current_price, broker_current_price: bp.current_price, action_taken: 'updated_holding' });
        } else {
          events.push({ event_type: 'matched', symbol: sym, app_qty: existing.shares, broker_qty: bp.shares, action_taken: 'none' });
        }
      }
    }

    // app -> broker (externally closed) — ingest the actual broker fill through the
    // lot path. If no authoritative exit fill can be identified, do NOT delete the
    // holding or close the lots — flag for review (fail-closed).
    for (const [sym, h] of Object.entries(appMap)) {
      if (!brokerMap[sym]) {
        let exitPrice = null;
        let realizedPnl = null;
        let actionTaken = 'closed_lots_and_removed';
        let eventType = 'externally_closed';
        let matchedActivity = null;

        try {
          const sinceDate = new Date(h.created_date || Date.now() - 30 * 86400000).toISOString().slice(0, 10);
          const activities = await getAlpacaActivities({ apiKey: cred.api_key, secretKey: cred.api_secret, mode: cred.mode, sinceDate });
          // Match by broker order ID or client order ID — NEVER by quantity alone.
          // Quantity matching can select the wrong transaction for frequently-traded
          // symbols; it only generates a candidate for manual review, never auto-settle.
          // Look up the app's Trade records for sells of this symbol to find
          // known broker order IDs — the Holding entity does not carry broker
          // order identity. (Fixes Rev.10 defect #4: the old code checked
          // h.broker_order_id / h.client_order_id on the Holding, which has
          // no such fields, so the match always failed and externally-closed
          // positions were always retained for review.)
          const symbolTrades = await sr.entities.Trade.filter({ user_id: user.id, symbol: sym, action: 'sell' });
          const tradeOrderIds = symbolTrades.map((t) => t.broker_order_id).filter(Boolean);
          const tradeClientOrderIds = symbolTrades.map((t) => t.client_order_id).filter(Boolean);
          matchedActivity = activities.find((a) => {
            if (String(a.symbol).toUpperCase() !== sym) return false;
            if (a.side !== 'sell') return false;
            if (a.order_id && tradeOrderIds.includes(a.order_id)) return true;
            if (a.order_client_id && tradeClientOrderIds.includes(a.order_client_id)) return true;
            return false;
          });
          if (matchedActivity) {
            exitPrice = Number(matchedActivity.price);
          } else {
            // No authoritative close fill found — fail-closed: retain the position.
            eventType = 'reconciliation_adjustment';
            actionTaken = 'flagged_for_review_no_exit_price';
          }
        } catch (e) {
          eventType = 'reconciliation_adjustment';
          actionTaken = 'flagged_for_review_activities_unreachable';
        }

        if (exitPrice && exitPrice > 0) {
          // Authoritative close — close lots FIFO at the real exit price.
          try {
            realizedPnl = await closeLotsFifo(sr, user.id, sym, h.shares, exitPrice, h);
          } catch (e) {
            // Lot close failed (e.g. insufficient lots) — do NOT delete the holding.
            await sr.entities.Holding.update(h.id, { reconciliation_blocked: true, reconciliation_blocked_reason: 'LOT_CLOSE_FAILED', reconciliation_blocked_at: nowIso() });
            blocked.push(sym);
            events.push({ event_type: 'reconciliation_adjustment', symbol: sym, app_qty: h.shares, app_avg_price: h.avg_price, action_taken: 'flagged_for_review_lot_close_failed', details: e.message });
            continue;
          }
          await sr.entities.Trade.create({
            user_id: user.id, symbol: h.symbol, company_name: h.company_name || h.symbol, action: 'sell', shares: h.shares,
            price: exitPrice, total_value: h.shares * exitPrice,
            notes: 'Reconciliation: position no longer held at broker (externally closed)',
            ai_recommended: false, order_status: 'reconciled_external', source: 'reconciliation', realized_pnl: realizedPnl,
          });
          await sr.entities.Holding.delete(h.id);
          removed.push(sym);
          events.push({ event_type: eventType, symbol: sym, app_qty: h.shares, app_avg_price: h.avg_price, action_taken: actionTaken, realized_pnl: realizedPnl, details: `Exit price from broker fill: $${exitPrice.toFixed(2)}` });
        } else {
          // FAIL-CLOSED: no authoritative exit fill. Retain the holding and lots.
          // Surface a review alert — do not finalize the accounting lifecycle.
          await sr.entities.Holding.update(h.id, { reconciliation_blocked: true, reconciliation_blocked_reason: 'NO_EXIT_PRICE', reconciliation_blocked_at: nowIso() });
          blocked.push(sym);
          events.push({ event_type: eventType, symbol: sym, app_qty: h.shares, app_avg_price: h.avg_price, action_taken: actionTaken, details: 'No broker fill activity found — exit price unknown, position retained for review' });
        }
      }
    }

    // Persist every reconciliation event (user-scoped)
    for (const e of events) {
      await sr.entities.ReconciliationEvent.create({ user_id: user.id, run_timestamp: runTs, ...e });
    }

    const summary = {
      matched: events.filter((e) => e.event_type === 'matched').length,
      qty_drift: events.filter((e) => e.event_type === 'qty_drift').length,
      price_drift: events.filter((e) => e.event_type === 'price_drift').length,
      new_from_broker: created.length,
      externally_closed: removed.length,
      adjustments: events.filter((e) => e.event_type === 'reconciliation_adjustment').length,
      blocked_for_review: blocked.length,
    };

    return Response.json({ ok: true, run_timestamp: runTs, broker_positions: brokerPositions.length, created: created.length, updated: updated.length, removed, blocked, events: events.length, summary });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}