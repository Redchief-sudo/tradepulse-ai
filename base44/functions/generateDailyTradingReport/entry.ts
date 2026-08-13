import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaAccount, getAlpacaActivities, getAlpacaPositions } from '../../shared/alpaca.ts';
import { getCashBalance } from '../../shared/cashLedger.ts';
import { nyDayStart, nySessionDateStr } from '../../shared/marketHours.ts';
import { calculateDailyReturn, indexClosedLotsByFillId } from '../../shared/dailyPerformance.ts';
import { brokerFillConservation, dailyReportStatus, reconciliationIsFresh } from '../../shared/operationalTruth.ts';

// Generates a comprehensive daily trading report — one TradingSession record
// per trading day that ties together trades, fills, scan runs, audit events,
// and AI decisions into a single auditable journal entry.
//
// Rev.13 audit fixes:
// 21. CLOSED SESSION IMUTABILITY: a 'closed' session is never overwritten.
//     Only 'open' sessions are updatable; calling with final=true on an already
//     closed session is a no-op.
// 22. PARTIAL FILL SEPARATION: fully filled, partially filled, pending, and
//     canceled remainder are counted separately — not lumped as "filled".
// 23. LOT-BASED HOLDING TIME: derived from closed PositionLots linked to the
//     sell fill, not from the latest prior buy timestamp.
// 24. AI DECISION ATTRIBUTION BY ID: matched via the TradeIntent's decision_id,
//     not just symbol+date — prevents wrong confidence/score attribution when
//     multiple decisions exist for one symbol.
// 25. ENTRY PRICE FROM LOTS: the weighted average acquisition cost of the
//     closed PositionLots, not the sell fill's filled_avg_price.
// 26. NEW YORK MARKET-SESSION DATES: the trading day is computed in
//     America/New_York, not UTC — so an 8 PM ET trade belongs to the same day.
// 27. BROKER-UNREACHABLE STATUS: when the broker is unreachable, the report
//     marks broker_data_status = 'unavailable' so a degraded report is visible.
// 28. CASH-BALANCE FAILURE: when cash balance can't be loaded, the report
//     marks itself incomplete instead of proceeding with null cash.

// NY session date helpers are in shared/marketHours.ts (nySessionDateStr).
// (Fixes Rev.13 #26: trading day is computed in America/New_York, not UTC.)

// Check if a timestamp falls within the NY market session day.
function inNySessionDay(ts, sessionDate) {
  if (!ts) return false;
  return nySessionDateStr(new Date(ts)) === sessionDate;
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const body = await req.json().catch(() => ({}));
    // Use NY session date — the report date represents the US market trading day.
    // (Fixes Rev.13 #26.)
    const reportDate = body.date || nySessionDateStr();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(reportDate)) {
      return Response.json({ error: 'INVALID_REPORT_DATE: expected YYYY-MM-DD in America/New_York' }, { status: 400 });
    }
    const sessionId = `session-${reportDate}`;
    const isFinal = body.final === true;

    // CLOSED SESSION IMUTABILITY: if the session is already closed, do not
    // overwrite it. A closed report is immutable — corrections require a new
    // amendment record. (Fixes Rev.13 #21.)
    const existingSessions = await sr.entities.TradingSession.filter({ user_id: user.id, session_id: sessionId });
    if (existingSessions.length > 0 && existingSessions[0].status === 'closed' && isFinal) {
      return Response.json({ ok: true, skipped: true, reason: 'Session already closed — immutable', session: existingSessions[0] });
    }
    const isExistingOpen = existingSessions.length > 0 && existingSessions[0].status === 'open';

    // Fetch all user-scoped data
    const [trades, intents, scanRuns, auditEvents, aiDecisions, fills, holdings, positionLots, settlementEvents, reconciliationEvents] = await Promise.all([
      sr.entities.Trade.filter({ user_id: user.id }),
      sr.entities.TradeIntent.filter({ user_id: user.id }),
      sr.entities.ScanRun.filter({ user_id: user.id }),
      sr.entities.AuditEvent.filter({ user_id: user.id }),
      sr.entities.AITradeDecision.filter({ user_id: user.id }),
      sr.entities.Fill.filter({ user_id: user.id }),
      sr.entities.Holding.filter({ user_id: user.id }),
      sr.entities.PositionLot.filter({ user_id: user.id }),
      sr.entities.SettlementEvent.filter({ user_id: user.id }),
      sr.entities.ReconciliationEvent.filter({ user_id: user.id }),
    ]);

    // Filter to the NY session day (Fixes Rev.13 #26)
    const dayTrades = trades.filter((t) => inNySessionDay(t.created_date, reportDate));
    const dayIntents = intents.filter((i) => inNySessionDay(i.created_date, reportDate));
    const dayScans = scanRuns.filter((s) => inNySessionDay(s.started_at, reportDate));
    const dayAudit = auditEvents.filter((a) => inNySessionDay(a.created_date, reportDate));
    const dayDecisions = aiDecisions.filter((a) => inNySessionDay(a.created_date, reportDate));
    const dayFills = fills.filter((f) => inNySessionDay(f.timestamp || f.created_date, reportDate));
    const daySettlements = settlementEvents.filter((e) => inNySessionDay(e.occurred_at || e.created_date, reportDate));
    const dayReconciliation = reconciliationEvents.filter((e) => inNySessionDay(e.run_timestamp || e.created_date, reportDate));
    if (isFinal && !dayAudit.some((e) => e.event_type === 'market_open_bot_started')) {
      return Response.json({ ok: false, skipped: true, report_type: 'final', report_status: 'incomplete', reason: 'No broker-authorized market-open marker for session date' }, { status: 409 });
    }

    // --- Equity calculations ---
    const appPositionValue = holdings.reduce((s, h) => s + Math.abs(h.shares) * (h.current_price || h.avg_price), 0);
    const costBasis = holdings.reduce((s, h) => s + Math.abs(h.shares) * h.avg_price, 0);
    const unrealizedPnl = holdings.reduce((s, h) => s + h.shares * ((h.current_price || h.avg_price) - h.avg_price), 0);

    let brokerEquity = null;
    let brokerPrevCloseEquity = null;
    let buyingPower = null;
    let brokerOpenPositions = null;
    let brokerDataStatus = 'not_applicable';
    let cashBalance = null;
    let brokerFills = [];
    let brokerMarketCloseAt = null;
    let brokerPositionSymbols = [];
    const brokerCreds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      brokerDataStatus = 'available';
      try {
        const connection = { apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode };
        const [acct, brokerPositions, activities] = await Promise.all([
          getAlpacaAccount(connection),
          getAlpacaPositions(connection),
          getAlpacaActivities({ ...connection, sinceDate: nyDayStart(new Date(`${reportDate}T12:00:00Z`)).toISOString(), pageSize: 100, direction: 'asc', paginate: true }),
        ]);
        brokerEquity = Number(acct.equity);
        brokerPrevCloseEquity = Number(acct.last_equity) || null;
        buyingPower = Number(acct.buying_power) || Number(acct.cash) || null;
        cashBalance = Number(acct.cash);
        brokerOpenPositions = brokerPositions.length;
        brokerPositionSymbols = brokerPositions.map((position) => String(position.symbol).toUpperCase());
        brokerFills = activities.filter((activity) => inNySessionDay(activity.transaction_time || activity.date || activity.created_at, reportDate));
        brokerMarketCloseAt = new Date(nyDayStart(new Date(`${reportDate}T12:00:00Z`)).getTime() + 16 * 60 * 60 * 1000).toISOString();
      } catch (e) {
        // BROKER-UNREACHABLE: mark the report as degraded so it's not mistaken
        // for an authoritative broker-sourced report. (Fixes Rev.13 #27.)
        brokerDataStatus = 'unavailable';
      }
    }

    // Cash balance (internal paper mode) — fail visibly, not silently.
    // (Fixes Rev.13 #28: cash balance loading failure was silently swallowed,
    // proceeding with null cash and making equity look authoritative.)
    let cashDataStatus = brokerCreds[0] ? 'not_applicable' : 'available';
    if (!brokerCreds[0]) {
      try { cashBalance = await getCashBalance(sr, user.id); }
      catch (e) { cashDataStatus = 'unavailable'; }
    }

    const appEquity = cashBalance == null ? null : appPositionValue + cashBalance;
    const priorClosedSessions = (await sr.entities.TradingSession.filter({ user_id: user.id }))
      .filter((s) => s.status === 'closed' && s.session_date < reportDate && s.ending_equity != null && Number.isFinite(Number(s.ending_equity)))
      .sort((a, b) => String(b.session_date).localeCompare(String(a.session_date)));
    const startingEquity = brokerCreds[0]
      ? (Number.isFinite(brokerPrevCloseEquity) ? brokerPrevCloseEquity : null)
      : (priorClosedSessions.length > 0 ? Number(priorClosedSessions[0].ending_equity) : null);
    const endingEquity = brokerCreds[0] ? brokerEquity : appEquity;
    const dailyReturnPct = calculateDailyReturn(startingEquity, endingEquity);

    // --- Realized P&L from sell trades ---
    const sellTrades = dayTrades.filter((t) => t.action === 'sell');
    const realizedPnl = sellTrades.reduce((s, t) => s + (t.realized_pnl || 0), 0);

    // --- Fees and commissions ---
    const commissionsTotal = dayFills.reduce((s, f) => s + (f.commission || 0), 0);
    const feesTotal = dayFills.reduce((s, f) => s + (f.fees || 0), 0);

    // --- Win/loss metrics ---
    const closedTrades = sellTrades;
    const winners = closedTrades.filter((t) => (t.realized_pnl || 0) > 0);
    const losers = closedTrades.filter((t) => (t.realized_pnl || 0) < 0);
    const winRatePct = closedTrades.length > 0 ? (winners.length / closedTrades.length) * 100 : 0;
    const avgWinner = winners.length > 0 ? winners.reduce((s, t) => s + t.realized_pnl, 0) / winners.length : 0;
    const avgLoser = losers.length > 0 ? losers.reduce((s, t) => s + t.realized_pnl, 0) / losers.length : 0;
    const largestWinner = winners.length > 0 ? Math.max(...winners.map((t) => t.realized_pnl)) : 0;
    const largestLoser = losers.length > 0 ? Math.min(...losers.map((t) => t.realized_pnl)) : 0;

    // --- Trade intent counts — SEPARATE partial fills from fully filled.
    // (Fixes Rev.13 #22: partially_filled was lumped with filled/settled.)
    const tradesFullyFilled = dayIntents.filter((i) => ['filled', 'settled'].includes(i.status)).length;
    const tradesPartiallyFilled = dayIntents.filter((i) => i.status === 'partially_filled').length;
    const tradesPending = dayIntents.filter((i) => ['submitted', 'accepted'].includes(i.status)).length;
    const tradesRejected = dayIntents.filter((i) => i.status === 'rejected' || i.status === 'failed').length;
    const tradesCanceled = dayIntents.filter((i) => i.status === 'canceled' || i.status === 'expired').length;
    const tradesSubmitted = dayIntents.length;

    // --- Risk events ---
    const riskEventTypes = ['daily_loss_breach', 'kill_switch_activated', 'stale_order', 'stale_data_block', 'broker_outage', 'duplicate_attempt', 'max_drawdown_breach'];
    const riskEvents = dayAudit.filter((a) => riskEventTypes.includes(a.event_type));
    const killSwitchEvents = dayAudit.filter((a) => a.event_type === 'kill_switch_activated' || a.event_type === 'daily_loss_breach');

    // --- Reconciliation status ---
    const blockedHoldings = holdings.filter((h) => h.reconciliation_blocked);
    const unresolvedSettlements = daySettlements.filter((e) => e.status !== 'completed');
    const latestReconciliationBySymbol = {};
    for (const event of dayReconciliation) {
      const prior = latestReconciliationBySymbol[event.symbol];
      if (!prior || new Date(event.run_timestamp || event.created_date) > new Date(prior.run_timestamp || prior.created_date)) {
        latestReconciliationBySymbol[event.symbol] = event;
      }
    }
    const driftEvents = Object.values(latestReconciliationBySymbol).filter((e) => e.event_type !== 'matched');
    const latestReconciliationAt = dayReconciliation.reduce((latest, event) => {
      const timestamp = event.run_timestamp || event.created_date;
      return !latest || new Date(timestamp) > new Date(latest) ? timestamp : latest;
    }, null);
    const lastBrokerFillAt = brokerFills.reduce((latest, fill) => {
      const timestamp = fill.transaction_time || fill.date || fill.created_at;
      return timestamp && (!latest || new Date(timestamp) > new Date(latest)) ? timestamp : latest;
    }, null);
    const reconciliationFresh = !brokerCreds[0] || reconciliationIsFresh(latestReconciliationAt, lastBrokerFillAt, isFinal ? brokerMarketCloseAt : null);
    const relevantBrokerSymbols = new Set([...brokerPositionSymbols, ...brokerFills.map((fill) => String(fill.symbol || '').toUpperCase()).filter(Boolean)]);
    const missingReconciliationSymbols = [...relevantBrokerSymbols].filter((symbol) => !latestReconciliationBySymbol[symbol]);
    const hasReconciliationEvidence = brokerCreds[0]
      ? dayReconciliation.length > 0 && reconciliationFresh && missingReconciliationSymbols.length === 0
      : daySettlements.length > 0 || dayIntents.length === 0;
    const reconciliationStatus = blockedHoldings.length > 0
      ? `${blockedHoldings.length} blocked position(s): ${blockedHoldings.map((h) => h.symbol).join(', ')}`
      : unresolvedSettlements.length > 0
        ? `${unresolvedSettlements.length} unresolved settlement(s)`
        : driftEvents.length > 0
          ? `${driftEvents.length} reconciliation drift event(s)`
          : hasReconciliationEvidence ? 'clean' : 'not_verified';

    const dispositionValues = dayScans.flatMap((scan) => {
      try {
        const parsed = JSON.parse(scan.candidate_dispositions || '[]');
        return Array.isArray(parsed) ? parsed.map((entry) => entry.disposition) : Object.values(parsed);
      }
      catch (error) { return []; }
    });
    const candidatesDiscovered = dayScans.reduce((sum, scan) => sum + (Number(scan.candidates_found) || 0), 0);
    const candidatesFiltered = dispositionValues.filter((value) => String(value).startsWith('filtered:')).length;
    const candidatesVetoed = dispositionValues.filter((value) => String(value).startsWith('vetoed:')).length;
    const signalsIneligible = dispositionValues.filter((value) => String(value).startsWith('ineligible:') || String(value).startsWith('skipped:')).length;
    const unexplainedCandidates = Math.max(0, candidatesDiscovered - dispositionValues.length);
    const settlementCompleted = daySettlements.filter((event) => event.status === 'completed' && event.integrity_verified === true).length;
    const settlementPending = daySettlements.filter((event) => ['pending', 'processing', 'retryable_failed'].includes(event.status)).length;
    const settlementIntegrityBlocked = daySettlements.filter((event) => event.status === 'integrity_blocked').length;
    const settlementFailed = daySettlements.filter((event) => ['terminal_failed'].includes(event.status)).length;
    const settlementEventKeys = new Set(daySettlements.flatMap((event) => [event.event_id, event.broker_fill_id].filter(Boolean)));
    const unaccountedBrokerFills = dayFills.filter((fill) => !settlementEventKeys.has(fill.fill_id) && !settlementEventKeys.has(fill.broker_fill_id)).length;
    const brokerFillSummary = brokerCreds[0] ? brokerFillConservation(brokerFills, dayFills) : { alpaca_fill_count: 0, ledger_fill_count: dayFills.length, missing_ledger_fills: 0, extra_ledger_fills: 0, ok: true };
    const financialIntegrityBlocked = user.trading_session_state === 'financial_integrity_blocked' || user.financial_integrity_manual_reenable_required === true || settlementIntegrityBlocked > 0;
    const scanCyclesCompleted = dayScans.filter((scan) => ['completed', 'no_candidates'].includes(scan.status)).length;
    const scanCyclesFailed = dayScans.filter((scan) => ['failed', 'broker_unavailable'].includes(scan.status)).length;
    const healthCheckFailures = dayAudit.filter((event) => String(event.event_type).startsWith('health_check_') && ['warning', 'critical', 'error'].includes(event.severity)).length;
    const providerFailureMap = {};
    for (const event of dayAudit.filter((candidate) => /provider|market_data|snapshot_capture_failed/.test(String(candidate.event_type)))) {
      providerFailureMap[event.event_type] = (providerFailureMap[event.event_type] || 0) + 1;
    }
    const reportStatus = dailyReportStatus({
      financialIntegrityBlocked, settlementIntegrityBlocked, reconciliationStatus,
      unaccountedBrokerFills, missingLedgerFills: brokerFillSummary.missing_ledger_fills, extraLedgerFills: brokerFillSummary.extra_ledger_fills, positionDriftCount: driftEvents.length,
      brokerDataUnavailable: brokerDataStatus === 'unavailable', cashDataUnavailable: cashDataStatus === 'unavailable',
      settlementPending, settlementFailed, unexplainedCandidates, healthCheckFailures,
    });

    // --- Sector exposure ---
    const sectorMap = {};
    let totalExposure = 0;
    holdings.forEach((h) => {
      const val = Math.abs(h.shares) * (h.current_price || h.avg_price);
      const s = h.sector || 'Other';
      sectorMap[s] = (sectorMap[s] || 0) + val;
      totalExposure += val;
    });
    const sectorExposure = Object.entries(sectorMap).map(([sector, value]) => ({
      sector, value, percent: totalExposure > 0 ? (value / totalExposure) * 100 : 0,
    }));

    // --- Model version and regime from latest scan ---
    const latestScan = dayScans.sort((a, b) => new Date(b.started_at) - new Date(a.started_at))[0];

    // --- Max drawdown (from PnlRecord if available, else from equity delta) ---
    let maxDrawdownPct = null;
    const pnlRecords = await sr.entities.PnlRecord.filter({ user_id: user.id, date: reportDate });
    if (pnlRecords.length > 1) {
      maxDrawdownPct = 0;
      const sorted = pnlRecords.sort((a, b) => new Date(a.timestamp || a.created_date) - new Date(b.timestamp || b.created_date));
      let peak = sorted[0].equity || 0;
      for (const r of sorted) {
        const eq = r.equity || 0;
        if (eq > peak) peak = eq;
        const dd = peak > 0 ? ((peak - eq) / peak) * 100 : 0;
        if (dd > maxDrawdownPct) maxDrawdownPct = dd;
      }
    }

    // --- Build enriched per-trade log with LOT-BASED attribution ---
    // (Fixes Rev.13 #23, #24, #25: holding time, AI decision, and entry price
    // are now derived from PositionLots and decision_id, not symbol-matching.)
    const sortedDayTrades = [...dayTrades].sort((a, b) => new Date(a.created_date) - new Date(b.created_date));
    let runningPnl = 0;

    // Build TradeIntent maps for canonical trade_intent_id attribution.
    const intentByClientId = {};
    intents.forEach((i) => { if (i.client_order_id) intentByClientId[i.client_order_id] = i; });
    const intentByBrokerOrderId = {};
    intents.forEach((i) => { if (i.broker_order_id) intentByBrokerOrderId[i.broker_order_id] = i; });

    // Build a map of closed lots by closure_fill_id for lot-based attribution.
    // (Fixes Rev.13 #23, #25.)
    const { index: closedLotsByFillId, malformedLotIds } = indexClosedLotsByFillId(positionLots);
    for (const lotId of malformedLotIds) {
      await sr.entities.AuditEvent.create({
        user_id: user.id,
        event_type: 'daily_report_integrity_degraded',
        severity: 'warning',
        entity_type: 'PositionLot',
        entity_id: lotId,
        message: 'Daily report excluded malformed PositionLot closure allocation evidence',
      });
    }

    const tradeLog = sortedDayTrades.map((t) => {
      const pnl = t.realized_pnl || 0;
      if (t.action === 'sell') runningPnl += pnl;
      const outcomeLabel = t.action === 'sell'
        ? (pnl > 0 ? 'winner' : pnl < 0 ? 'loser' : 'breakeven')
        : 'open';

      // Find the matching TradeIntent for this trade (by client_order_id or broker_order_id)
      const matchedIntent = intentByClientId[t.client_order_id] || intentByBrokerOrderId[t.broker_order_id] || null;

      // HOLDING TIME + ENTRY PRICE from closed PositionLots.
      // (Fixes Rev.13 #23, #25: was using latest prior buy timestamp and sell
      // fill's filled_avg_price as entry price.)
      let holdingTimeMinutes = null;
      let entryPrice = null;
      if (t.action === 'sell') {
        // Find fills for this sell trade
        const sellFills = fills.filter((f) =>
          (f.broker_order_id === t.broker_order_id || f.client_order_id === t.client_order_id) && f.side === 'sell'
        );
        // For each sell fill, find the closed lots and compute holding time + entry price
        const allClosedLots = [];
        for (const sf of sellFills) {
          const lots = closedLotsByFillId[sf.fill_id] || [];
          allClosedLots.push(...lots);
        }
        if (allClosedLots.length > 0) {
          // Holding time: from earliest lot acquisition to sell fill timestamp
          const earliestAcquisition = Math.min(...allClosedLots.map((l) => new Date(l.acquisition_timestamp).getTime()));
          const sellTime = sellFills.length > 0
            ? Math.min(...sellFills.map((f) => new Date(f.timestamp || f.created_date).getTime()))
            : new Date(t.created_date).getTime();
          holdingTimeMinutes = Math.round((sellTime - earliestAcquisition) / 60000);

          // PER-FILL LOT ALLOCATION: use the per-fill closure quantities stored in
          // closure_fill_ids, not the lot's total opened-remaining delta.
          // (Fixes Rev.15 #11: the old code used quantity_opened - quantity_remaining
          // for the ENTIRE lot for EACH closure fill, double-counting when a lot
          // was closed by multiple sell fills.)
          const sellFillIds = new Set(sellFills.map((f) => f.fill_id));
          const parseClosureFills = (cfid) => {
            const parsed = JSON.parse(cfid || '[]');
            if (parsed.length === 0) return [];
            if (typeof parsed[0] === 'string') return parsed.map((fid) => ({ fill_id: fid, qty: null }));
            return parsed;
          };
          const totalQty = allClosedLots.reduce((s, l) => {
            const allocations = parseClosureFills(l.closure_fill_ids);
            return s + allocations
              .filter((a) => sellFillIds.has(a.fill_id))
              .reduce((sum, a) => sum + (a.qty || 0), 0);
          }, 0);
          const totalCost = allClosedLots.reduce((s, l) => {
            const allocations = parseClosureFills(l.closure_fill_ids);
            const qty = allocations
              .filter((a) => sellFillIds.has(a.fill_id))
              .reduce((sum, a) => sum + (a.qty || 0), 0);
            return s + qty * l.acquisition_price;
          }, 0);
          entryPrice = totalQty > 0 ? totalCost / totalQty : null;
        }
      }

      // Exit price from linked Fill records, not the order-summary Trade.price.
      // (Fixes Rev.14 #21: the order-summary price can conceal fill-level
      // execution detail for partial fills. The authoritative exit price
      // comes from the linked Fill records.)
      let exitPrice = null;
      if (t.action === 'sell') {
        const sellFills = fills.filter((f) =>
          (f.broker_order_id === t.broker_order_id || f.client_order_id === t.client_order_id) && f.side === 'sell'
        );
        if (sellFills.length > 0) {
          const totalNotional = sellFills.reduce((s, f) => s + (f.filled_quantity || 0) * (f.filled_price || 0), 0);
          const totalQty = sellFills.reduce((s, f) => s + (f.filled_quantity || 0), 0);
          exitPrice = totalQty > 0 ? totalNotional / totalQty : null;
        }
      }
      // EXIT PRICE: no fallback to Trade.price. If no linked Fill records exist,
      // the exit price is marked as unavailable — a supposedly authoritative daily
      // journal should not silently substitute the order-summary price.
      // (Fixes Rev.15 #12: the fallback concealed fill-level execution detail.)

      // Fallback entry price for buys: use the fill price
      if (entryPrice == null && t.action === 'buy') entryPrice = t.filled_avg_price || t.price || null;

      // AI decisions are projections keyed by canonical trade_intent_id.
      let aiDecision = null;
      if (matchedIntent?.trade_intent_id) aiDecision = aiDecisions.find((d) => d.trade_intent_id === matchedIntent.trade_intent_id);

      // Execution latency from fills
      const tradeFills = fills.filter((f) => f.broker_order_id === t.broker_order_id || f.client_order_id === t.client_order_id);
      const tradeSettlements = settlementEvents.filter((event) => event.broker_order_id === t.broker_order_id || event.client_order_id === t.client_order_id || event.trade_intent_id === matchedIntent?.trade_intent_id);
      const avgLatency = tradeFills.length > 0
        ? Math.round(tradeFills.reduce((s, f) => s + (f.fill_latency_ms || 0), 0) / tradeFills.length)
        : null;

      return {
        trade_id: t.id,
        timestamp: t.created_date,
        symbol: t.symbol,
        side: t.action,
        quantity: t.filled_qty || t.shares,
        entry_price: entryPrice,
        exit_price: exitPrice,
        exit_price_source: exitPrice != null ? 'fill_ledger' : 'unavailable',
        realized_pnl: pnl,
        running_daily_pnl: t.action === 'sell' ? runningPnl : null,
        outcome_label: outcomeLabel,
        holding_time_minutes: holdingTimeMinutes,
        execution_latency_ms: avgLatency,
        commission: t.commission || 0,
        broker_order_id: t.broker_order_id,
        scan_run_id: matchedIntent?.idempotency_key?.startsWith('autonomous-')
          ? matchedIntent.idempotency_key.slice('autonomous-'.length, -(`-${t.symbol}-${t.action}`.length))
          : null,
        trade_intent_id: matchedIntent?.trade_intent_id || null,
        requested_quantity: matchedIntent?.requested_quantity || null,
        filled_quantity: matchedIntent?.filled_quantity || t.filled_qty || t.shares,
        average_fill_price: matchedIntent?.filled_avg_price || null,
        fill_ids: tradeFills.map((fill) => fill.fill_id),
        settlement_event_ids: tradeSettlements.map((event) => event.event_id),
        broker_status: matchedIntent?.broker_terminal_status || matchedIntent?.status || null,
        settlement_status: tradeSettlements.length > 0 && tradeSettlements.every((event) => event.status === 'completed' && event.integrity_verified === true) ? 'completed' : tradeSettlements[0]?.status || 'missing',
        fees: tradeFills.reduce((sum, fill) => sum + (Number(fill.fees) || 0), 0),
        decision_id: aiDecision?.id || matchedIntent?.decision_id || null,
        ai_confidence: aiDecision?.confidence || null,
        ml_score: aiDecision?.ml_score || null,
        risk_score: aiDecision?.risk_score || null,
        market_regime: matchedIntent?.regime || aiDecision?.regime || null,
        model_version: latestScan?.model_version || null,
        reconciliation_status: t.order_status === 'reconciled_external' ? 'reconciled' : 'direct',
      };
    });

    // --- Assemble session data ---
    const reportDegraded = brokerDataStatus === 'unavailable'
      || cashDataStatus === 'unavailable'
      || reconciliationStatus !== 'clean'
      || tradesPending > 0
      || malformedLotIds.length > 0;
    const canFinalize = isFinal && reportStatus === 'healthy' && !reportDegraded;
    const sessionData = {
      session_id: sessionId,
      session_date: reportDate,
      status: isFinal ? 'closed' : 'open',
      report_status: reportStatus,
      starting_equity: startingEquity,
      ending_equity: endingEquity,
      broker_equity: brokerEquity,
      broker_prev_close_equity: brokerPrevCloseEquity,
      app_equity: appEquity,
      daily_return_pct: dailyReturnPct == null ? null : Math.round(dailyReturnPct * 100) / 100,
      realized_pnl: Math.round(realizedPnl * 100) / 100,
      unrealized_pnl: Math.round(unrealizedPnl * 100) / 100,
      fees_total: Math.round(feesTotal * 100) / 100,
      commissions_total: Math.round(commissionsTotal * 100) / 100,
      buying_power: buyingPower,
      cash_balance: cashBalance,
      max_drawdown_pct: maxDrawdownPct == null ? null : Math.round(maxDrawdownPct * 100) / 100,
      num_scans: dayScans.length,
      num_ai_decisions: dayDecisions.length,
      trades_submitted: tradesSubmitted,
      trades_filled: tradesFullyFilled,
      trades_partially_filled: tradesPartiallyFilled,
      trades_pending: tradesPending,
      trades_rejected: tradesRejected,
      trades_canceled: tradesCanceled,
      win_rate_pct: Math.round(winRatePct * 100) / 100,
      num_winners: winners.length,
      num_losers: losers.length,
      avg_winner: Math.round(avgWinner * 100) / 100,
      avg_loser: Math.round(avgLoser * 100) / 100,
      largest_winner: Math.round(largestWinner * 100) / 100,
      largest_loser: Math.round(largestLoser * 100) / 100,
      num_risk_events: riskEvents.length,
      num_kill_switch_events: killSwitchEvents.length,
      reconciliation_status: reconciliationStatus,
      broker_data_status: brokerDataStatus,
      cash_data_status: cashDataStatus,
      unresolved_settlements: unresolvedSettlements.length,
      malformed_lot_allocations: malformedLotIds.length,
      model_version: latestScan?.model_version || null,
      market_regime: latestScan?.market_regime || null,
      sector_exposure: JSON.stringify(sectorExposure),
      open_positions: brokerOpenPositions ?? holdings.length,
      trade_ids: JSON.stringify(sortedDayTrades.map((t) => t.id)),
      scan_run_ids: JSON.stringify(dayScans.map((s) => s.id)),
      generated_at: new Date().toISOString(),
      broker_day_pnl: startingEquity != null && endingEquity != null ? endingEquity - startingEquity : null,
      broker_cash: brokerCreds[0] ? cashBalance : null,
      broker_position_count: brokerOpenPositions,
      ledger_position_count: holdings.length,
      candidates_discovered: candidatesDiscovered,
      candidates_filtered: candidatesFiltered,
      candidates_vetoed: candidatesVetoed,
      signals_ineligible: signalsIneligible,
      broker_fill_count: brokerFillSummary.alpaca_fill_count,
      alpaca_fill_count: brokerFillSummary.alpaca_fill_count,
      ledger_fill_count: brokerFillSummary.ledger_fill_count,
      missing_ledger_fills: brokerFillSummary.missing_ledger_fills,
      extra_ledger_fills: brokerFillSummary.extra_ledger_fills,
      latest_reconciliation_at: latestReconciliationAt,
      reconciliation_fresh: reconciliationFresh,
      missing_reconciliation_symbols: JSON.stringify(missingReconciliationSymbols),
      settlement_completed: settlementCompleted,
      settlement_pending: settlementPending,
      settlement_failed: settlementFailed,
      settlement_integrity_blocked: settlementIntegrityBlocked,
      unaccounted_broker_fills: unaccountedBrokerFills,
      unexplained_candidates: unexplainedCandidates,
      position_drift_count: driftEvents.length,
      cash_reconciliation_status: brokerCreds[0] ? 'broker_authoritative' : cashDataStatus,
      financial_integrity_status: financialIntegrityBlocked ? 'blocked' : unresolvedSettlements.length > 0 ? 'unresolved' : 'healthy',
      scan_cycles_started: dayScans.length,
      scan_cycles_completed: scanCyclesCompleted,
      scan_cycles_failed: scanCyclesFailed,
      health_check_failures: healthCheckFailures,
      provider_failure_counts: JSON.stringify(providerFailureMap),
    };

    // --- Create or update (idempotent for open sessions only) ---
    // CLOSED SESSION IMUTABILITY: a closed session is never overwritten.
    // (Fixes Rev.13 #21.)
    let session;
    if (isExistingOpen) {
      session = await sr.entities.TradingSession.update(existingSessions[0].id, sessionData);
    } else if (existingSessions.length === 0) {
      session = await sr.entities.TradingSession.create({ user_id: user.id, ...sessionData });
    } else {
      // Already closed and not final — return existing
      session = existingSessions[0];
    }

    return Response.json({
      ok: !isFinal || canFinalize,
      report_date: reportDate,
      report_type: isFinal ? 'final' : 'intraday',
      generated_at: session.generated_at,
      session,
      trades: tradeLog,
      scan_runs: dayScans.map((s) => ({
        scan_run_id: s.scan_run_id,
        started_at: s.started_at,
        completed_at: s.completed_at,
        status: s.status,
        candidates_found: s.candidates_found,
        proposals_created: s.proposals_created,
        trades_filled: s.trades_filled,
        market_regime: s.market_regime,
        model_version: s.model_version,
      })),
      audit_events: dayAudit.map((a) => ({
        timestamp: a.created_date,
        event_type: a.event_type,
        severity: a.severity,
        message: a.message,
      })),
      // Report quality indicators (Fixes Rev.13 #27, #28)
      report_quality: {
        broker_data_status: brokerDataStatus,
        cash_data_status: cashDataStatus,
        degraded: reportDegraded,
        finalized: canFinalize,
        finalization_blocked: isFinal && !canFinalize,
      },
      // Partial fill breakdown (Fixes Rev.13 #22)
      trade_breakdown: {
        fully_filled: tradesFullyFilled,
        partially_filled: tradesPartiallyFilled,
        pending: tradesPending,
        rejected: tradesRejected,
        canceled: tradesCanceled,
      },
    }, { status: isFinal && !canFinalize ? 409 : 200 });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
