import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaAccount } from '../../shared/alpaca.ts';
import { getCashBalance } from '../../shared/cashLedger.ts';

// Generates a comprehensive daily trading report — one immutable TradingSession
// record per trading day that ties together trades, fills, scan runs, audit
// events, and AI decisions into a single auditable journal entry.
//
// Idempotent: calling with the same date updates the existing session record.
// Can be called on-demand from the UI or automatically at market close via
// the "Daily Trading Report" workflow.
//
// Returns the session summary + enriched per-trade log with computed fields
// (outcome label, holding time, running daily P&L, etc.).
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const body = await req.json().catch(() => ({}));
    const reportDate = body.date || new Date().toISOString().slice(0, 10);
    const sessionId = `session-${reportDate}`;
    const isFinal = body.final === true;

    // Date range for the session
    const dayStart = new Date(reportDate + 'T00:00:00.000Z');
    const dayEnd = new Date(reportDate + 'T23:59:59.999Z');
    const inRange = (d) => {
      if (!d) return false;
      const t = new Date(d).getTime();
      return t >= dayStart.getTime() && t <= dayEnd.getTime();
    };

    // Fetch all user-scoped data
    const [trades, intents, scanRuns, auditEvents, aiDecisions, fills, holdings] = await Promise.all([
      sr.entities.Trade.filter({ user_id: user.id }),
      sr.entities.TradeIntent.filter({ user_id: user.id }),
      sr.entities.ScanRun.filter({ user_id: user.id }),
      sr.entities.AuditEvent.filter({ user_id: user.id }),
      sr.entities.AITradeDecision.filter({ user_id: user.id }),
      sr.entities.Fill.filter({ user_id: user.id }),
      sr.entities.Holding.filter({ user_id: user.id }),
    ]);

    // Filter to the session day
    const dayTrades = trades.filter((t) => inRange(t.created_date));
    const dayIntents = intents.filter((i) => inRange(i.created_date));
    const dayScans = scanRuns.filter((s) => inRange(s.started_at));
    const dayAudit = auditEvents.filter((a) => inRange(a.created_date));
    const dayDecisions = aiDecisions.filter((a) => inRange(a.created_date));
    const dayFills = fills.filter((f) => inRange(f.timestamp || f.created_date));

    // --- Equity calculations ---
    const appEquity = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    const costBasis = holdings.reduce((s, h) => s + h.shares * h.avg_price, 0);
    const unrealizedPnl = appEquity - costBasis;

    let brokerEquity = null;
    let brokerPrevCloseEquity = null;
    let buyingPower = null;
    const brokerCreds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
        brokerEquity = Number(acct.equity);
        brokerPrevCloseEquity = Number(acct.last_equity) || null;
        buyingPower = Number(acct.buying_power) || Number(acct.cash) || null;
      } catch (e) { /* broker unreachable — use app estimate */ }
    }

    // Cash balance (internal paper mode)
    let cashBalance = null;
    if (!brokerCreds[0]) {
      try { cashBalance = await getCashBalance(sr, user.id); } catch (e) {}
    }

    const startingEquity = brokerPrevCloseEquity || brokerEquity || appEquity;
    const endingEquity = brokerEquity || (appEquity + (cashBalance || 0));
    const dailyReturnPct = startingEquity > 0 ? ((endingEquity - startingEquity) / startingEquity) * 100 : 0;

    // --- Realized P&L from sell trades ---
    const sellTrades = dayTrades.filter((t) => t.action === 'sell');
    const realizedPnl = sellTrades.reduce((s, t) => s + (t.realized_pnl || 0), 0);

    // --- Fees and commissions ---
    const commissionsTotal = dayFills.reduce((s, f) => s + (f.commission || 0), 0);
    const feesTotal = dayFills.reduce((s, f) => s + (f.fees || 0), 0);

    // --- Win/loss metrics ---
    const closedTrades = sellTrades.filter((t) => (t.realized_pnl || 0) !== 0);
    const winners = closedTrades.filter((t) => (t.realized_pnl || 0) > 0);
    const losers = closedTrades.filter((t) => (t.realized_pnl || 0) < 0);
    const winRatePct = closedTrades.length > 0 ? (winners.length / closedTrades.length) * 100 : 0;
    const avgWinner = winners.length > 0 ? winners.reduce((s, t) => s + t.realized_pnl, 0) / winners.length : 0;
    const avgLoser = losers.length > 0 ? losers.reduce((s, t) => s + t.realized_pnl, 0) / losers.length : 0;
    const largestWinner = winners.length > 0 ? Math.max(...winners.map((t) => t.realized_pnl)) : 0;
    const largestLoser = losers.length > 0 ? Math.min(...losers.map((t) => t.realized_pnl)) : 0;

    // --- Trade intent counts ---
    const tradesSubmitted = dayIntents.length;
    const tradesFilled = dayIntents.filter((i) => ['filled', 'settled', 'partially_filled'].includes(i.status)).length;
    const tradesRejected = dayIntents.filter((i) => i.status === 'rejected' || i.status === 'failed').length;
    const tradesCanceled = dayIntents.filter((i) => i.status === 'canceled' || i.status === 'expired').length;

    // --- Risk events ---
    const riskEventTypes = ['daily_loss_breach', 'kill_switch_activated', 'stale_order', 'stale_data_block', 'broker_outage', 'duplicate_attempt'];
    const riskEvents = dayAudit.filter((a) => riskEventTypes.includes(a.event_type));
    const killSwitchEvents = dayAudit.filter((a) => a.event_type === 'kill_switch_activated' || a.event_type === 'daily_loss_breach');

    // --- Reconciliation status ---
    const blockedHoldings = holdings.filter((h) => h.reconciliation_blocked);
    const reconciliationStatus = blockedHoldings.length === 0
      ? 'clean'
      : `${blockedHoldings.length} blocked position(s): ${blockedHoldings.map((h) => h.symbol).join(', ')}`;

    // --- Sector exposure ---
    const sectorMap = {};
    let totalExposure = 0;
    holdings.forEach((h) => {
      const val = h.shares * (h.current_price || h.avg_price);
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
    let maxDrawdownPct = 0;
    const pnlRecords = await sr.entities.PnlRecord.filter({ user_id: user.id, date: reportDate });
    if (pnlRecords.length > 1) {
      const sorted = pnlRecords.sort((a, b) => new Date(a.timestamp || a.created_date) - new Date(b.timestamp || b.created_date));
      let peak = sorted[0].equity || 0;
      for (const r of sorted) {
        const eq = r.equity || 0;
        if (eq > peak) peak = eq;
        const dd = peak > 0 ? ((peak - eq) / peak) * 100 : 0;
        if (dd > maxDrawdownPct) maxDrawdownPct = dd;
      }
    }

    // --- Build enriched per-trade log ---
    // Sort trades by time, compute running daily P&L and outcome labels
    const sortedDayTrades = [...dayTrades].sort((a, b) => new Date(a.created_date) - new Date(b.created_date));
    let runningPnl = 0;
    const tradeLog = sortedDayTrades.map((t) => {
      const pnl = t.realized_pnl || 0;
      if (t.action === 'sell') runningPnl += pnl;
      const outcomeLabel = t.action === 'sell'
        ? (pnl > 0 ? 'winner' : pnl < 0 ? 'loser' : 'breakeven')
        : 'open';

      // Holding time: find the matching buy fill for this symbol
      let holdingTimeMinutes = null;
      if (t.action === 'sell') {
        const buyFills = fills.filter((f) => f.symbol === t.symbol && f.side === 'buy' && new Date(f.timestamp || f.created_date) < new Date(t.created_date));
        if (buyFills.length > 0) {
          const lastBuy = buyFills.sort((a, b) => new Date(b.timestamp || b.created_date) - new Date(a.timestamp || a.created_date))[0];
          holdingTimeMinutes = Math.round((new Date(t.created_date) - new Date(lastBuy.timestamp || lastBuy.created_date)) / 60000);
        }
      }

      // Execution latency from fills
      const tradeFills = fills.filter((f) => f.broker_order_id === t.broker_order_id || f.client_order_id === t.client_order_id);
      const avgLatency = tradeFills.length > 0
        ? Math.round(tradeFills.reduce((s, f) => s + (f.fill_latency_ms || 0), 0) / tradeFills.length)
        : null;

      // AI decision data
      const aiDecision = aiDecisions.find((d) => d.symbol === t.symbol && inRange(d.created_date));

      return {
        trade_id: t.id,
        timestamp: t.created_date,
        symbol: t.symbol,
        side: t.action,
        quantity: t.filled_qty || t.shares,
        entry_price: t.filled_avg_price || t.price,
        exit_price: t.action === 'sell' ? t.price : null,
        realized_pnl: pnl,
        running_daily_pnl: t.action === 'sell' ? runningPnl : null,
        outcome_label: outcomeLabel,
        holding_time_minutes: holdingTimeMinutes,
        execution_latency_ms: avgLatency,
        commission: t.commission || 0,
        broker_order_id: t.broker_order_id,
        ai_confidence: aiDecision?.confidence || null,
        ml_score: aiDecision?.ml_score || null,
        risk_score: aiDecision?.risk_score || null,
        market_regime: latestScan?.market_regime || null,
        model_version: latestScan?.model_version || null,
        reconciliation_status: t.order_status === 'reconciled_external' ? 'reconciled' : 'direct',
      };
    });

    // --- Assemble session data ---
    const sessionData = {
      session_id: sessionId,
      session_date: reportDate,
      status: isFinal ? 'closed' : 'open',
      starting_equity: startingEquity,
      ending_equity: endingEquity,
      broker_equity: brokerEquity,
      broker_prev_close_equity: brokerPrevCloseEquity,
      app_equity: appEquity,
      daily_return_pct: Math.round(dailyReturnPct * 100) / 100,
      realized_pnl: Math.round(realizedPnl * 100) / 100,
      unrealized_pnl: Math.round(unrealizedPnl * 100) / 100,
      fees_total: Math.round(feesTotal * 100) / 100,
      commissions_total: Math.round(commissionsTotal * 100) / 100,
      buying_power: buyingPower,
      cash_balance: cashBalance,
      max_drawdown_pct: Math.round(maxDrawdownPct * 100) / 100,
      num_scans: dayScans.length,
      num_ai_decisions: dayDecisions.length,
      trades_submitted: tradesSubmitted,
      trades_filled: tradesFilled,
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
      model_version: latestScan?.model_version || null,
      market_regime: latestScan?.market_regime || null,
      sector_exposure: JSON.stringify(sectorExposure),
      open_positions: holdings.length,
      trade_ids: JSON.stringify(sortedDayTrades.map((t) => t.id)),
      scan_run_ids: JSON.stringify(dayScans.map((s) => s.id)),
      generated_at: new Date().toISOString(),
    };

    // --- Create or update (idempotent) ---
    const existing = await sr.entities.TradingSession.filter({ user_id: user.id, session_id: sessionId });
    let session;
    if (existing.length > 0) {
      session = await sr.entities.TradingSession.update(existing[0].id, sessionData);
    } else {
      session = await sr.entities.TradingSession.create({ user_id: user.id, ...sessionData });
    }

    return Response.json({
      ok: true,
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
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}