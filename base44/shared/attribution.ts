// Deterministic performance attribution + execution observability.
//
// All metrics are computed from the immutable ledgers (Trade, Fill, TradeIntent,
// Holding) using exact arithmetic — no LLM estimation. This is the analytical
// layer that answers "which strategies make money?" and "is the execution
// pipeline healthy?".

export interface StrategyRow {
  strategy: string;
  trades: number;
  buys: number;
  sells: number;
  realizedPnl: number;
  commissions: number;
  netPnl: number;
  notional: number;
  wins: number;
  losses: number;
  grossWin: number;
  grossLoss: number;
  winRate: number;
  profitFactor: number;
  avgWin: number;
  avgLoss: number;
  openPnl: number;
  totalPnl: number;
}

export interface AssetClassRow {
  assetClass: string;
  trades: number;
  sells: number;
  wins: number;
  realizedPnl: number;
  openPnl: number;
  netPnl: number;
  notional: number;
  commissions: number;
  fees: number;
  slippage: number;
  winRate: number;
  totalPnl: number;
}

export interface VenueRow {
  venue: string;
  fills: number;
  notional: number;
  commissions: number;
  fees: number;
  slippage: number;
}

export interface ExecutionHealth {
  totalIntents: number;
  byStatus: Record<string, number>;
  filled: number;
  rejected: number;
  canceled: number;
  fillRate: number;
  rejectionRate: number;
  rejectionReasons: { reason: string; count: number }[];
  avgLatencyMs: number;
  p95LatencyMs: number;
  byVenue: VenueRow[];
  totalCost: number;
  totalSlippage: number;
}

function safeNum(n: any): number {
  const v = Number(n);
  return Number.isFinite(v) ? v : 0;
}

// Decompose realized + open P&L by originating strategy (Trade.source / Fill.strategy_id).
export function computeStrategyAttribution(
  trades: any[],
  fills: any[],
  holdings: any[]
): StrategyRow[] {
  const byStrategy: Record<string, StrategyRow> = {};

  function row(strategy: string): StrategyRow {
    if (!byStrategy[strategy]) {
      byStrategy[strategy] = {
        strategy, trades: 0, buys: 0, sells: 0, realizedPnl: 0, commissions: 0,
        netPnl: 0, notional: 0, wins: 0, losses: 0, grossWin: 0, grossLoss: 0,
        winRate: 0, profitFactor: 0, avgWin: 0, avgLoss: 0, openPnl: 0, totalPnl: 0,
      };
    }
    return byStrategy[strategy];
  }

  // Realized P&L + cost + notional from the Trade ledger.
  for (const t of trades) {
    const strat = t.source || t.strategy_id || 'manual';
    const s = row(strat);
    s.trades++;
    s.commissions += safeNum(t.commission);
    s.notional += safeNum(t.total_value || (safeNum(t.shares) * safeNum(t.price)));
    if (t.action === 'buy') s.buys++;
    if (t.action === 'sell') {
      s.sells++;
      const pnl = safeNum(t.realized_pnl);
      s.realizedPnl += pnl;
      if (pnl > 0) { s.wins++; s.grossWin += pnl; }
      else if (pnl < 0) { s.losses++; s.grossLoss += Math.abs(pnl); }
    }
  }

  // Open P&L attribution: use the opening-side fill for the current direction
  // (buy for longs, sell for shorts) and attribute unrealized P&L to it.
  const fillsBySymbol: Record<string, any[]> = {};
  for (const f of fills) {
    const sym = String(f.symbol).toUpperCase();
    if (!fillsBySymbol[sym]) fillsBySymbol[sym] = [];
    fillsBySymbol[sym].push(f);
  }
  for (const h of holdings) {
    const sym = String(h.symbol).toUpperCase();
    const openingSide = safeNum(h.shares) < 0 ? 'sell' : 'buy';
    const openingFills = (fillsBySymbol[sym] || []).filter((f) => f.side === openingSide);
    if (!openingFills.length) continue;
    const latest = openingFills.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())[openingFills.length - 1];
    const strat = latest.strategy_id || 'manual';
    const cur = safeNum(h.current_price || h.avg_price);
    const openPnl = safeNum(h.shares) * (cur - safeNum(h.avg_price));
    row(strat).openPnl += openPnl;
  }

  // Derived metrics.
  for (const s of Object.values(byStrategy)) {
    s.winRate = s.sells > 0 ? (s.wins / s.sells) * 100 : 0;
    s.profitFactor = s.grossLoss > 0 ? s.grossWin / s.grossLoss : (s.grossWin > 0 ? 99 : 0);
    s.avgWin = s.wins > 0 ? s.grossWin / s.wins : 0;
    s.avgLoss = s.losses > 0 ? s.grossLoss / s.losses : 0;
    s.netPnl = s.realizedPnl - s.commissions;
    s.totalPnl = s.netPnl + s.openPnl;
  }

  return Object.values(byStrategy).sort((a, b) => b.totalPnl - a.totalPnl);
}

// Execution-pipeline health from the TradeIntent state machine + Fill ledger.
export function computeExecutionHealth(intents: any[], fills: any[]): ExecutionHealth {
  const totalIntents = intents.length;
  const byStatus: Record<string, number> = {};
  for (const i of intents) {
    byStatus[i.status] = (byStatus[i.status] || 0) + 1;
  }
  const filled = (byStatus.filled || 0) + (byStatus.settled || 0) + (byStatus.partially_filled || 0);
  const rejected = (byStatus.rejected || 0) + (byStatus.failed || 0);
  const canceled = byStatus.canceled || 0;
  const fillRate = totalIntents > 0 ? (filled / totalIntents) * 100 : 0;
  const rejectionRate = totalIntents > 0 ? ((rejected + canceled) / totalIntents) * 100 : 0;

  // Rejection-reason breakdown (top reasons).
  const reasonMap: Record<string, number> = {};
  for (const i of intents) {
    if (!i.rejection_reason) continue;
    const r = String(i.rejection_reason).split(':')[0].split('(')[0].trim().slice(0, 60);
    reasonMap[r] = (reasonMap[r] || 0) + 1;
  }
  const rejectionReasons = Object.entries(reasonMap)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 8);

  // Signal-to-fill latency (signal_timestamp → Fill.timestamp).
  const intentByTid: Record<string, any> = {};
  for (const i of intents) {
    if (i.trade_intent_id) intentByTid[i.trade_intent_id] = i;
  }
  const latencies: number[] = [];
  for (const f of fills) {
    const intent = intentByTid[f.trade_intent_id];
    if (!intent || !intent.signal_timestamp || !f.timestamp) continue;
    const ms = new Date(f.timestamp).getTime() - new Date(intent.signal_timestamp).getTime();
    if (ms >= 0 && ms < 86400000) latencies.push(ms);
  }
  latencies.sort((a, b) => a - b);
  const avgLatencyMs = latencies.length ? latencies.reduce((a, b) => a + b, 0) / latencies.length : 0;
  const p95LatencyMs = latencies.length ? latencies[Math.floor(latencies.length * 0.95)] : 0;

  // Cost / slippage by venue.
  const byVenueMap: Record<string, VenueRow> = {};
  for (const f of fills) {
    const v = f.venue || f.execution_mode || 'unknown';
    if (!byVenueMap[v]) byVenueMap[v] = { venue: v, fills: 0, notional: 0, commissions: 0, fees: 0, slippage: 0 };
    const x = byVenueMap[v];
    x.fills++;
    x.notional += safeNum(f.notional);
    x.commissions += safeNum(f.commission);
    x.fees += safeNum(f.fees);
    x.slippage += safeNum(f.slippage);
  }

  const totalCost = Object.values(byVenueMap).reduce((s, v) => s + v.commissions + v.fees, 0);
  const totalSlippage = Object.values(byVenueMap).reduce((s, v) => s + v.slippage, 0);

  return {
    totalIntents, byStatus, filled, rejected, canceled, fillRate, rejectionRate,
    rejectionReasons, avgLatencyMs, p95LatencyMs,
    byVenue: Object.values(byVenueMap).sort((a, b) => b.notional - a.notional),
    totalCost, totalSlippage,
  };
}

// Asset-class attribution: P&L decomposition by asset class (stocks, crypto, ...).
// Uses the Fill ledger for cost/notional/slippage, Holding for open P&L, and joins
// Trade → Fill on client_order_id for realized P&L attribution.
export function computeAssetClassAttribution(
  trades: any[],
  fills: any[],
  holdings: any[]
): AssetClassRow[] {
  const assetClassByOrderId: Record<string, string> = {};
  for (const f of fills) {
    if (f.client_order_id && f.asset_class) {
      assetClassByOrderId[f.client_order_id] = f.asset_class;
    }
  }

  const byAc: Record<string, AssetClassRow> = {};
  function row(ac: string): AssetClassRow {
    if (!byAc[ac]) {
      byAc[ac] = {
        assetClass: ac, trades: 0, sells: 0, wins: 0, realizedPnl: 0, openPnl: 0,
        netPnl: 0, notional: 0, commissions: 0, fees: 0, slippage: 0, winRate: 0, totalPnl: 0,
      };
    }
    return byAc[ac];
  }

  for (const t of trades) {
    const ac = assetClassByOrderId[t.client_order_id] || 'stocks';
    const s = row(ac);
    s.trades++;
    s.notional += safeNum(t.total_value || (safeNum(t.shares) * safeNum(t.price)));
    s.commissions += safeNum(t.commission);
    if (t.action === 'sell') {
      s.sells++;
      const pnl = safeNum(t.realized_pnl);
      s.realizedPnl += pnl;
      if (pnl > 0) s.wins++;
    }
  }

  for (const h of holdings) {
    const ac = h.asset_class || 'stocks';
    const s = row(ac);
    const cur = safeNum(h.current_price || h.avg_price);
    s.openPnl += safeNum(h.shares) * (cur - safeNum(h.avg_price));
  }

  for (const f of fills) {
    const ac = f.asset_class || 'stocks';
    const s = row(ac);
    s.fees += safeNum(f.fees);
    s.slippage += safeNum(f.slippage);
  }

  for (const s of Object.values(byAc)) {
    s.winRate = s.sells > 0 ? (s.wins / s.sells) * 100 : 0;
    s.netPnl = s.realizedPnl - s.commissions;
    s.totalPnl = s.netPnl + s.openPnl;
  }

  return Object.values(byAc).sort((a, b) => b.totalPnl - a.totalPnl);
}
