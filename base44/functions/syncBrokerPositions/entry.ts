import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { updateSessionState, SESSION_STATES } from '../../shared/sessionState.ts';
import { nowIso } from '../../shared/lotAccounting.ts';

// Broker-position reconciliation is deliberately read-only with respect to the
// financial ledger. Broker fills are ingested by runOrderReconciliation and all
// PositionLot, CashEntry, Holding, Trade, AITradeDecision, and TradeIntent
// projections are owned by processSettlementQueue.
//
// A position discrepancy is evidence that the canonical fill lifecycle is not
// settled. Record it and fail closed; never invent a fill, lot, price, or P&L
// from a broker position snapshot.

function normalizeBrokerPosition(position) {
  return {
    symbol: String(position.symbol || '').toUpperCase(),
    shares: Number(position.qty),
    avg_price: Number(position.avg_entry_price),
    current_price: Number(position.current_price),
    market_value: Number(position.market_value),
  };
}

function quantitiesDiffer(left, right) {
  return Math.abs((Number(left) || 0) - (Number(right) || 0)) > 0.0001;
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const runTimestamp = nowIso();
    const credentials = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    const credential = credentials.find((item) => item.broker === 'alpaca');
    if (!credential) {
      return Response.json({ error: 'Alpaca not connected — nothing to reconcile' }, { status: 400 });
    }

    const baseUrl = credential.mode === 'live'
      ? 'https://api.alpaca.markets/v2'
      : 'https://paper-api.alpaca.markets/v2';
    const headers = {
      'APCA-API-KEY-ID': credential.api_key,
      'APCA-API-SECRET-KEY': credential.api_secret,
    };

    let brokerPositions;
    try {
      const response = await fetch(`${baseUrl}/positions`, { headers });
      if (!response.ok) {
        const body = await response.text().catch(() => '');
        const details = `Alpaca HTTP ${response.status}: ${body}`;
        await sr.entities.ReconciliationEvent.create({
          user_id: user.id,
          run_timestamp: runTimestamp,
          event_type: 'broker_unreachable',
          symbol: '*',
          details,
          action_taken: 'flagged_for_review',
        });
        return Response.json({ error: details }, { status: 502 });
      }
      brokerPositions = await response.json();
    } catch (error) {
      await sr.entities.ReconciliationEvent.create({
        user_id: user.id,
        run_timestamp: runTimestamp,
        event_type: 'broker_unreachable',
        symbol: '*',
        details: error.message,
        action_taken: 'flagged_for_review',
      });
      return Response.json({ error: error.message }, { status: 502 });
    }

    const brokerMap = new Map(
      brokerPositions.map(normalizeBrokerPosition).map((position) => [position.symbol, position])
    );
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const holdingMap = new Map(
      holdings.map((holding) => [String(holding.symbol || '').toUpperCase(), holding])
    );
    const symbols = new Set([...brokerMap.keys(), ...holdingMap.keys()]);
    const events = [];
    const blocked = [];

    for (const symbol of symbols) {
      const broker = brokerMap.get(symbol);
      const holding = holdingMap.get(symbol);
      const brokerQuantity = broker?.shares || 0;
      const ledgerQuantity = Number(holding?.shares) || 0;

      if (quantitiesDiffer(brokerQuantity, ledgerQuantity)) {
        blocked.push(symbol);
        events.push({
          event_type: broker && holding
            ? 'qty_drift'
            : broker
              ? 'new_from_broker'
              : 'externally_closed',
          symbol,
          broker_qty: brokerQuantity,
          app_qty: ledgerQuantity,
          broker_avg_price: broker?.avg_price,
          app_avg_price: holding?.avg_price,
          details: 'Financial projections unchanged. Awaiting authoritative broker fill ingestion and canonical settlement replay.',
          action_taken: 'financial_integrity_blocked_pending_settlement',
        });
        continue;
      }

      events.push({
        event_type: 'matched',
        symbol,
        broker_qty: brokerQuantity,
        app_qty: ledgerQuantity,
        action_taken: 'none',
      });
    }

    for (const event of events) {
      await sr.entities.ReconciliationEvent.create({
        user_id: user.id,
        run_timestamp: runTimestamp,
        ...event,
      });
    }

    if (blocked.length > 0) {
      await updateSessionState(
        sr,
        user.id,
        SESSION_STATES.FINANCIAL_INTEGRITY_BLOCKED,
        `BROKER_LEDGER_POSITION_MISMATCH: ${blocked.join(',')}`
      );
    }

    return Response.json({
      ok: blocked.length === 0,
      run_timestamp: runTimestamp,
      broker_positions: brokerPositions.length,
      blocked,
      events: events.length,
      summary: {
        matched: events.filter((event) => event.event_type === 'matched').length,
        discrepancies: blocked.length,
        financial_writes: 0,
      },
    }, { status: blocked.length === 0 ? 200 : 409 });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
