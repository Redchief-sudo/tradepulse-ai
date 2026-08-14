// Deterministic, strategy-independent risk engine.
// A DENIED result means ZERO shares — never "at least one share".
// The risk engine has veto authority over every strategy and AI signal.
// All queries are user-scoped — no cross-user data leakage.

import { nyDayStart } from './marketHours.ts';

// Conservative engineering defaults — per the risk containment audit.
// max_risk_per_trade_pct: max loss per position as % of equity (risk-based sizing)
// max_total_exposure_pct: max total invested exposure as % of equity
// max_simultaneous_orders: max outstanding (non-terminal) orders at once
const RISK_LIMITS = {
  aggressive: {
    max_position_pct: 15, max_sector_pct: 40, min_confidence: 70, max_daily_trades: 8,
    stop_loss_pct: 12, max_drawdown_pct: 25,
    max_open_positions: 12, max_daily_loss_pct: 5,
    spread_limit_pct: 2, slippage_limit_pct: 1.5,
    max_risk_per_trade_pct: 0.50, max_total_exposure_pct: 60, max_simultaneous_orders: 5,
  },
  balanced: {
    max_position_pct: 7, max_sector_pct: 20, min_confidence: 80, max_daily_trades: 3,
    stop_loss_pct: 8, max_drawdown_pct: 15,
    max_open_positions: 5, max_daily_loss_pct: 1.0,
    spread_limit_pct: 1.5, slippage_limit_pct: 1,
    max_risk_per_trade_pct: 0.30, max_total_exposure_pct: 40, max_simultaneous_orders: 2,
  },
  conservative: {
    max_position_pct: 5, max_sector_pct: 15, min_confidence: 88, max_daily_trades: 2,
    stop_loss_pct: 5, max_drawdown_pct: 8,
    max_open_positions: 3, max_daily_loss_pct: 0.5,
    spread_limit_pct: 1, slippage_limit_pct: 0.5,
    max_risk_per_trade_pct: 0.25, max_total_exposure_pct: 30, max_simultaneous_orders: 2,
  },
  micro: {
    max_position_pct: 20, max_sector_pct: 50, min_confidence: 82, max_daily_trades: 2,
    stop_loss_pct: 6, max_drawdown_pct: 10,
    max_open_positions: 3, max_daily_loss_pct: 2,
    spread_limit_pct: 2, slippage_limit_pct: 1,
    max_risk_per_trade_pct: 1.0, max_total_exposure_pct: 70, max_simultaneous_orders: 2,
  },
};

export function riskLimitsForProfile(profileId) {
  return RISK_LIMITS[profileId] || RISK_LIMITS.balanced;
}

// Build a user-scoped portfolio snapshot for risk evaluation.
// accountEquity: when provided (broker_paper/live), overrides holdings-based equity
// with the real broker account equity so position/sector caps use the actual capital base.
// brokerPrevCloseEquity: when provided (broker_paper/live), the daily P&L percentage
// is computed from broker equity decline (current vs previous close) instead of
// app-realized sells. This ensures the canonical risk engine uses the SAME daily-loss
// definition as the scan cycle for broker-backed users, eliminating contradictory
// risk decisions. (Fixes Rev.15 #9: scan used broker equity, execution used app P&L.)
export async function buildPortfolioSnapshot(sr, userId, accountEquity = null, brokerPrevCloseEquity = null) {
  const holdings = await sr.entities.Holding.filter({ user_id: userId });
  const holdingsEquity = holdings.reduce(
    (s, h) => s + h.shares * (h.current_price || h.avg_price), 0
  );
  const totalEquity = accountEquity && accountEquity > 0 ? accountEquity : holdingsEquity;
  const sectorMap = {};
  holdings.forEach((h) => {
    const sec = h.sector || 'Other';
    sectorMap[sec] = (sectorMap[sec] || 0) + Math.abs(h.shares * (h.current_price || h.avg_price));
  });
  // Filter trades server-side by date to avoid fetching entire trade history on every risk check.
  // Use NY market session midnight, not server-local midnight — a trade at 8 PM ET
  // on Monday belongs to Monday's session. (Fixes Rev.13 #20.)
  const startOfDay = nyDayStart();
  const recentTrades = await sr.entities.Trade.filter({
    user_id: userId,
    created_date: { $gte: startOfDay.toISOString() }
  }, '-created_date', 100);
  const tradesToday = recentTrades.filter((t) => new Date(t.created_date) >= startOfDay);

  // DAILY P&L: use broker equity decline when available (broker mode), otherwise
  // app-realized sells (internal paper). (Fixes Rev.15 #9.)
  let dailyPnlPct;
  if (brokerPrevCloseEquity && brokerPrevCloseEquity > 0 && accountEquity && accountEquity > 0) {
    // Broker mode: daily P&L = (current equity - prev close) / prev close
    dailyPnlPct = ((accountEquity - brokerPrevCloseEquity) / brokerPrevCloseEquity) * 100;
  } else {
    // Internal paper mode: daily P&L = realized sells / equity
    const dailyRealized = tradesToday
      .filter((t) => t.action === 'sell')
      .reduce((s, t) => s + (t.realized_pnl || 0), 0);
    dailyPnlPct = totalEquity > 0 ? (dailyRealized / totalEquity) * 100 : 0;
  }

  // Total invested exposure and outstanding orders — for the new risk controls.
  const totalExposure = holdings.reduce((s, h) => s + Math.abs(h.shares * (h.current_price || h.avg_price)), 0);
  const totalExposurePct = totalEquity > 0 ? (totalExposure / totalEquity) * 100 : 0;
  const allIntents = await sr.entities.TradeIntent.filter({ user_id: userId });
  const outstandingOrders = allIntents.filter((i) => ['submitted', 'accepted', 'partially_filled'].includes(i.status)).length;
  return { holdings, totalEquity, sectorMap, openPositions: holdings.length, tradesToday: tradesToday.length, dailyPnlPct, totalExposure, totalExposurePct, outstandingOrders };
}

// Floor at asset-appropriate precision so a cap never increases the requested
// notional. Equities use thousandths; crypto uses eight decimal places.
function roundQty(q, assetClass = 'stocks') {
  const precision = String(assetClass).toLowerCase() === 'crypto' ? 100000000 : 1000;
  return Math.floor(Math.max(0, q) * precision) / precision;
}

// evaluateRisk → { approved, approvedQuantity, reasons, snapshot }
export function evaluateRisk(intent, snapshot, limits, opts = {}) {
  const reasons = [];
  const { totalEquity, sectorMap, openPositions, tradesToday, dailyPnlPct, holdings } = snapshot;
  const symbol = String(intent.symbol || '').toUpperCase();
  const qty = Number(intent.requested_quantity) || 0;
  const price = Number(intent.limit_price || intent.price) || 0;
  const side = intent.side;
  const assetClass = intent.asset_class || 'stocks';
  const minimumQuantity = String(assetClass).toLowerCase() === 'crypto' ? 0.00000001 : 0.001;

  if (opts.killSwitch && !opts.protectiveExit) {
    return { approved: false, approvedQuantity: 0, reasons: ['KILL_SWITCH_ACTIVE'], snapshot };
  }

  // Spread limit check — fail closed if bid/ask unavailable or spread excessive.
  // (Fixes Rev.13 #16: spread_limit_pct was defined but never enforced.)
  // Internal paper / shadow mode have no real market data — skip with a
  // documented exemption. Broker mode must pass bid/ask or be rejected.
  if (side === 'buy' && limits.spread_limit_pct && !opts.skipMarketDataChecks) {
    if (opts.bid == null || opts.ask == null) {
      reasons.push('NO_QUOTE_DATA_FOR_SPREAD_CHECK');
    } else {
      const mid = (opts.bid + opts.ask) / 2;
      if (mid > 0) {
        const spreadPct = ((opts.ask - opts.bid) / mid) * 100;
        if (spreadPct > limits.spread_limit_pct) {
          reasons.push(`SPREAD_EXCEEDS_LIMIT (${spreadPct.toFixed(2)}% > ${limits.spread_limit_pct}%)`);
        }
      }
    }
  }

  // Slippage limit check — fail closed if no estimate is supplied.
  // (Fixes Rev.14 #12: slippage failed open when no estimate was supplied,
  // allowing trades to bypass the configured slippage limit.)
  // Internal paper / shadow mode have no real market data — skip with a
  // documented exemption. Broker mode must supply an estimate or be rejected.
  if (side === 'buy' && limits.slippage_limit_pct && !opts.skipMarketDataChecks) {
    if (opts.estimated_slippage_pct == null) {
      reasons.push('NO_SLIPPAGE_ESTIMATE');
    } else if (opts.estimated_slippage_pct > limits.slippage_limit_pct) {
      reasons.push(`SLIPPAGE_EXCEEDS_LIMIT (${opts.estimated_slippage_pct.toFixed(2)}% > ${limits.slippage_limit_pct}%)`);
    }
  }

  // Max drawdown check — block new buys when drawdown exceeds the profile limit.
  // (Fixes Rev.14 #13: max drawdown was only enforced in the scan cycle, not
  // in the canonical risk engine. Other trade surfaces could bypass it.)
  // Sells are always permitted (risk management always runs).
  if (side === 'buy' && !opts.protectiveExit && opts.maxDrawdownBreached) {
    reasons.push('MAX_DRAWDOWN_BREACHED');
  }

  if (opts.protectiveExit) {
    return reasons.length
      ? { approved: false, approvedQuantity: 0, reasons, snapshot }
      : { approved: true, approvedQuantity: qty, reasons: ['PROTECTIVE_EXIT'], snapshot };
  }

  if (side === 'sell' && !opts.protectiveExit) {
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
  if (!opts.protectiveExit && openPositions >= limits.max_open_positions) {
    reasons.push(`MAX_OPEN_POSITIONS_REACHED (${openPositions}/${limits.max_open_positions})`);
  }
  if (dailyPnlPct <= -limits.max_daily_loss_pct) {
    reasons.push(`MAX_DAILY_LOSS_EXCEEDED (${dailyPnlPct.toFixed(2)}% <= -${limits.max_daily_loss_pct}%)`);
  }
  // Total exposure check — reject if adding this position would exceed the max
  if (limits.max_total_exposure_pct && snapshot.totalExposurePct != null && totalEquity > 0) {
    const newExposurePct = snapshot.totalExposurePct + (qty * price / totalEquity) * 100;
    if (newExposurePct > limits.max_total_exposure_pct) {
      reasons.push(`MAX_TOTAL_EXPOSURE_EXCEEDED (${newExposurePct.toFixed(1)}% > ${limits.max_total_exposure_pct}%)`);
    }
  }
  // Outstanding orders check — reject if too many simultaneous open orders
  if (limits.max_simultaneous_orders && snapshot.outstandingOrders != null) {
    if (snapshot.outstandingOrders >= limits.max_simultaneous_orders) {
      reasons.push(`MAX_SIMULTANEOUS_ORDERS_REACHED (${snapshot.outstandingOrders}/${limits.max_simultaneous_orders})`);
    }
  }
  if (reasons.length) {
    return { approved: false, approvedQuantity: 0, reasons, snapshot };
  }

  let approvedQty = qty;
  if (!opts.protectiveExit && totalEquity > 0 && price > 0) {
    // RISK-BASED SIZING — size from risk budget / (entry - stop), not from
    // a blind position percentage. This limits loss by stop distance.
    // (Per the audit: shares = risk_budget / risk_per_share, then cap by
    // max_position_pct, max_sector_pct, and total exposure.)
    if (side === 'buy' && intent.stop_loss > 0 && limits.max_risk_per_trade_pct) {
      const riskBudget = (limits.max_risk_per_trade_pct / 100) * totalEquity;
      const riskPerShare = price - intent.stop_loss;
      if (riskPerShare > 0) {
        const riskBasedQty = roundQty(riskBudget / riskPerShare, assetClass);
        if (riskBasedQty < approvedQty) {
          approvedQty = riskBasedQty;
          reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_RISK_BASED_SIZING`);
        }
      }
    }
    const maxPositionNotional = (limits.max_position_pct / 100) * totalEquity;
    if (approvedQty * price > maxPositionNotional) {
      approvedQty = roundQty(maxPositionNotional / price, assetClass);
      reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_MAX_POSITION_PCT`);
    }
    const sector = intent.sector || 'Other';
    const currentSector = sectorMap[sector] || 0;
    const maxSectorNotional = (limits.max_sector_pct / 100) * totalEquity;
    const remainingSector = maxSectorNotional - currentSector;
    if (approvedQty * price > remainingSector) {
      const cappedBySector = price > 0 ? roundQty(remainingSector / price, assetClass) : 0;
      if (cappedBySector < approvedQty) {
        approvedQty = cappedBySector;
        reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_MAX_SECTOR_PCT`);
      }
    }
    // Total exposure cap — don't exceed the max total invested exposure
    if (limits.max_total_exposure_pct && snapshot.totalExposurePct != null) {
      const remainingExposure = ((limits.max_total_exposure_pct / 100) * totalEquity) - (snapshot.totalExposure || 0);
      if (approvedQty * price > remainingExposure) {
        const cappedByExposure = price > 0 ? roundQty(remainingExposure / price, assetClass) : 0;
        if (cappedByExposure < approvedQty) {
          approvedQty = cappedByExposure;
          reasons.push(`POSITION_CAPPED_TO_${approvedQty}_BY_MAX_TOTAL_EXPOSURE`);
        }
      }
    }
  }

  if (approvedQty < minimumQuantity) {
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
// (Fixes Rev.13 #31: queries by symbol instead of loading 200 global snapshots
// and searching in memory — the requested symbol may not be in the newest 200.)
export async function checkDataFreshness(sr, symbol, maxAgeMinutes = 5) {
  const sym = String(symbol).toUpperCase();
  const snaps = await sr.entities.PriceSnapshot.filter({ symbol: sym }, '-timestamp', 1);
  const latest = snaps && snaps[0];
  if (!latest) return { fresh: false, reason: 'NO_PRICE_SNAPSHOT', ageMinutes: null };
  const ts = latest.provider_timestamp || latest.timestamp;
  const ageMinutes = (Date.now() - new Date(ts).getTime()) / 60000;
  if (ageMinutes > maxAgeMinutes) {
    return { fresh: false, reason: 'STALE_MARKET_DATA', ageMinutes: Math.round(ageMinutes) };
  }
  return { fresh: true, ageMinutes: Math.round(ageMinutes) };
}

// Check max drawdown against historical peak equity. (Fixes Rev.13 #15.)
// Uses PnlRecord equity snapshots to find the peak and compute current drawdown.
// Blocks new buys when the profile threshold is reached — a pre-trade runtime
// control, not just a retrospective report metric.
export async function checkMaxDrawdown(sr, userId, currentEquity, limits) {
  if (!limits.max_drawdown_pct) return { breached: false };
  const records = await sr.entities.PnlRecord.filter({ user_id: userId }, '-date', 100);
  let peak = currentEquity;
  for (const r of records) {
    if (r.equity && r.equity > peak) peak = r.equity;
  }
  if (peak <= 0) return { breached: false };
  const drawdownPct = ((peak - currentEquity) / peak) * 100;
  return {
    breached: drawdownPct >= limits.max_drawdown_pct,
    drawdown_pct: Math.round(drawdownPct * 100) / 100,
    peak_equity: peak,
    limit: limits.max_drawdown_pct,
  };
}
