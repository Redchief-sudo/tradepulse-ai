// Deterministic, strategy-independent risk engine.
// A DENIED result means ZERO shares — never "at least one share".
// The risk engine has veto authority over every strategy and AI signal.
// All queries are user-scoped — no cross-user data leakage.

const RISK_LIMITS = {
  aggressive: {
    max_position_pct: 15, max_sector_pct: 40, min_confidence: 70, max_daily_trades: 8,
    stop_loss_pct: 12, max_drawdown_pct: 25,
    max_open_positions: 12, max_daily_loss_pct: 5, max_outstanding_orders: 20,
    spread_limit_pct: 2, slippage_limit_pct: 1.5,
  },
  balanced: {
    max_position_pct: 10, max_sector_pct: 25, min_confidence: 80, max_daily_trades: 5,
    stop_loss_pct: 8, max_drawdown_pct: 15,
    max_open_positions: 8, max_daily_loss_pct: 3, max_outstanding_orders: 12,
    spread_limit_pct: 1.5, slippage_limit_pct: 1,
  },
  conservative: {
    max_position_pct: 5, max_sector_pct: 15, min_confidence: 88, max_daily_trades: 3,
    stop_loss_pct: 5, max_drawdown_pct: 8,
    max_open_positions: 5, max_daily_loss_pct: 1.5, max_outstanding_orders: 8,
    spread_limit_pct: 1, slippage_limit_pct: 0.5,
  },
};

export function riskLimitsForProfile(profileId) {
  return RISK_LIMITS[profileId] || RISK_LIMITS.balanced;
}

// Build a user-scoped portfolio snapshot for risk evaluation.
// accountEquity: when provided (broker_paper/live), overrides holdings-based equity
// with the real broker account equity so position/sector caps use the actual capital base.
export async function buildPortfolioSnapshot(sr, userId, accountEquity = null) {
  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const holdingsEquity = holdings.reduce(
    (s, h) => s + h.shares * (h.current_price || h.avg_price), 0
  );
  const totalEquity = accountEquity && accountEquity > 0 ? accountEquity : holdingsEquity;
  const sectorMap = {};
  holdings.forEach((h) => {
    const sec = h.sector || 'Other';
    sectorMap[sec] = (sectorMap[sec] || 0) + h.shares * (h.current_price || h.avg_price);
  });
  // Filter trades server-side by date to avoid fetching entire trade history on every risk check.
  const startOfDay = new Date(); startOfDay.setHours(0, 0, 0, 0);
  const recentTrades = await sr.entities.Trade.filter({
    user_id: userId,
    created_date: { $gte: startOfDay.toISOString() }
  }, '-created_date', 100);
  const tradesToday = recentTrades.filter((t) => new Date(t.created_date) >= startOfDay);
  const dailyRealized = tradesToday
    .filter((t) => t.action === 'sell')
    .reduce((s, t) => s + (t.realized_pnl || 0), 0);
  const dailyPnlPct = totalEquity > 0 ? (dailyRealized / totalEquity) * 100 : 0;
  return { holdings, totalEquity, sectorMap, openPositions: holdings.length, tradesToday: tradesToday.length, dailyPnlPct };
}

// evaluateRisk → { approved, approvedQuantity, reasons, snapshot }
export function evaluateRisk(intent, snapshot, limits, opts = {}) {
  const reasons = [];
  const { totalEquity, sectorMap, openPositions, tradesToday, dailyPnlPct, holdings } = snapshot;
  const symbol = String(intent.symbol || '').toUpperCase();
  const qty = Number(intent.requested_quantity) || 0;
  const price = Number(intent.limit_price || intent.price) || 0;
  const side = intent.side;

  if (opts.killSwitch) {
    return { approved: false, approvedQuantity: 0, reasons: ['KILL_SWITCH_ACTIVE'], snapshot };
  }

  if (side === 'sell') {
    const existing = holdings.find((h) => String(h.symbol).toUpperCase() === symbol);
    if (!existing || existing.shares < qty) {
      const held = existing ? existing.shares : 0;
      return { approved: false, approvedQuantity: 0, reasons: [`INSUFFICIENT_POSITION_TO_SELL (held ${held}, requested ${qty})`], snapshot };
    }
    return { approved: true, approvedQuantity: qty, reasons: ['OK'], snapshot };
  }

  if (intent.confidence != null && intent.confidence < limits.min_confidence) {
    reasons.push(`CONFIDENCE_BELOW_MIN (${intent.confidence} < ${limits.min_confidence})`);
  }
  if (tradesToday >= limits.max_daily_trades) {
    reasons.push(`MAX_DAILY_TRADES_REACHED (${tradesToday}/${limits.max_daily_trades})`);
  }
  if (openPositions >= limits.max_open_positions) {
    reasons.push(`MAX_OPEN_POSITIONS_REACHED (${openPositions}/${limits.max_open_positions})`);
  }
  if (dailyPnlPct <= -limits.max_daily_loss_pct) {
    reasons.push(`MAX_DAILY_LOSS_EXCEEDED (${dailyPnlPct.toFixed(2)}% <= -${limits.max_daily_loss_pct}%)`);
  }
  if (reasons.length) {
    return { approved: false, approvedQuantity: 0, reasons, snapshot };
  }

  let approvedQty = qty;
  if (totalEquity > 0 && price > 0) {
    const maxPositionNotional = (limits.max_position_pct / 100) * totalEquity;
    if (qty * price > maxPositionNotional) {
      approvedQty = Math.floor(maxPositionNotional / price);
      reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_MAX_POSITION_PCT`);
    }
    const sector = intent.sector || 'Other';
    const currentSector = sectorMap[sector] || 0;
    const maxSectorNotional = (limits.max_sector_pct / 100) * totalEquity;
    const remainingSector = maxSectorNotional - currentSector;
    if (approvedQty * price > remainingSector) {
      const cappedBySector = price > 0 ? Math.floor(remainingSector / price) : 0;
      if (cappedBySector < approvedQty) {
        approvedQty = cappedBySector;
        reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_MAX_SECTOR_PCT`);
      }
    }
  }

  if (approvedQty < 1) {
    reasons.push('INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT');
    return { approved: false, approvedQuantity: 0, reasons, snapshot };
  }

  if (approvedQty >= qty) reasons.push('OK');
  return { approved: true, approvedQuantity: Math.max(0, approvedQty), reasons, snapshot };
}

// Market-data freshness guard (shared market data — not user-scoped).
// Freshness is based on the PROVIDER's observation timestamp when available,
// not the database insertion timestamp — so an after-hours close fetched at
// 9pm is not mistaken for a fresh quote. Note: the execution gateway now
// fetches the authoritative quote directly and does not depend on this check
// for new candidates; this remains for secondary/defensive freshness validation.
export async function checkDataFreshness(sr, symbol, maxAgeMinutes = 5) {
  const snaps = await sr.entities.PriceSnapshot.list('-timestamp', 200);
  const sym = String(symbol).toUpperCase();
  const latest = snaps.find((s) => String(s.symbol).toUpperCase() === sym);
  if (!latest) return { fresh: false, reason: 'NO_PRICE_SNAPSHOT', ageMinutes: null };
  const ts = latest.provider_timestamp || latest.timestamp;
  const ageMinutes = (Date.now() - new Date(ts).getTime()) / 60000;
  if (ageMinutes > maxAgeMinutes) {
    return { fresh: false, reason: 'STALE_MARKET_DATA', ageMinutes: Math.round(ageMinutes) };
  }
  return { fresh: true, ageMinutes: Math.round(ageMinutes) };
}