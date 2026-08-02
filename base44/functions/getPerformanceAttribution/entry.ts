import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { computeStrategyAttribution, computeExecutionHealth, computeAssetClassAttribution } from '../../shared/attribution.ts';

// Performance attribution + execution observability.
// Returns deterministic P&L decomposition by strategy and execution-pipeline
// health metrics, all derived from the immutable ledgers (Trade, Fill,
// TradeIntent, Holding). No LLM estimation — pure arithmetic.
export default async function (req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const [trades, fills, intents, holdings] = await Promise.all([
      sr.entities.Trade.filter({ user_id: user.id }, '-created_date', 1000),
      sr.entities.Fill.filter({ user_id: user.id }, '-created_date', 1000),
      sr.entities.TradeIntent.filter({ user_id: user.id }, '-created_date', 1000),
      sr.entities.Holding.filter({ user_id: user.id }),
    ]);

    const strategies = computeStrategyAttribution(trades, fills, holdings);
    const health = computeExecutionHealth(intents, fills);
    const byAssetClass = computeAssetClassAttribution(trades, fills, holdings);

    const totals = {
      realizedPnl: strategies.reduce((s, r) => s + r.realizedPnl, 0),
      openPnl: strategies.reduce((s, r) => s + r.openPnl, 0),
      netPnl: strategies.reduce((s, r) => s + r.netPnl, 0),
      totalPnl: strategies.reduce((s, r) => s + r.totalPnl, 0),
      commissions: strategies.reduce((s, r) => s + r.commissions, 0),
      notional: strategies.reduce((s, r) => s + r.notional, 0),
      trades: strategies.reduce((s, r) => s + r.trades, 0),
    };

    return Response.json({
      ok: true,
      strategies,
      health,
      byAssetClass,
      totals,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}