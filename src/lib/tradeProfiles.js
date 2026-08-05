// Institutional-grade trading risk profiles.
// Each profile enforces hard risk limits applied during AI scanning AND execution.

export const TRADE_PROFILES = {
  aggressive: {
    id: 'aggressive',
    name: 'Aggressive',
    description:
      'Maximum growth potential with higher risk tolerance. Larger positions, wider sector concentration, and a lower confidence floor for entry.',
    risk_level: 'High',
    max_position_pct: 15,
    max_sector_pct: 40,
    min_confidence: 70,
    max_daily_trades: 8,
    stop_loss_pct: 12,
    max_drawdown_pct: 25,
    max_daily_loss_pct: 5,
    max_risk_per_trade_pct: 0.50,
    max_total_exposure_pct: 60,
    max_simultaneous_orders: 5,
  },
  balanced: {
    id: 'balanced',
    name: 'Balanced',
    description:
      'Moderate risk with diversified exposure. Standard institutional position sizes and sector limits with a solid confidence floor.',
    risk_level: 'Medium',
    max_position_pct: 7,
    max_sector_pct: 20,
    min_confidence: 80,
    max_daily_trades: 3,
    stop_loss_pct: 8,
    max_drawdown_pct: 15,
    max_daily_loss_pct: 1.0,
    max_risk_per_trade_pct: 0.30,
    max_total_exposure_pct: 40,
    max_simultaneous_orders: 2,
  },
  conservative: {
    id: 'conservative',
    name: 'Conservative',
    description:
      'Capital preservation first. Smaller positions, strict sector diversification, and high confidence requirements. Ideal for risk-averse strategies.',
    risk_level: 'Low',
    max_position_pct: 5,
    max_sector_pct: 15,
    min_confidence: 88,
    max_daily_trades: 2,
    stop_loss_pct: 5,
    max_drawdown_pct: 8,
    max_daily_loss_pct: 0.5,
    max_risk_per_trade_pct: 0.25,
    max_total_exposure_pct: 30,
    max_simultaneous_orders: 2,
  },
  micro: {
    id: 'micro',
    name: 'Micro',
    description:
      'Optimized for small accounts ($100-$2,000). Concentrated positions with fractional shares, tight risk per trade, and a high confidence floor. Fewer but better trades.',
    risk_level: 'Medium',
    max_position_pct: 20,
    max_sector_pct: 50,
    min_confidence: 82,
    max_daily_trades: 2,
    stop_loss_pct: 6,
    max_drawdown_pct: 10,
    max_daily_loss_pct: 2,
    max_risk_per_trade_pct: 1.0,
    max_total_exposure_pct: 70,
    max_simultaneous_orders: 2,
  },
};

export const DEFAULT_PROFILE = 'balanced';

export function getProfile(id) {
  return TRADE_PROFILES[id] || TRADE_PROFILES[DEFAULT_PROFILE];
}

export function profileParams(id) {
  const p = getProfile(id);
  return {
    max_position_pct: p.max_position_pct,
    max_sector_pct: p.max_sector_pct,
    min_confidence: p.min_confidence,
    max_daily_trades: p.max_daily_trades,
    stop_loss_pct: p.stop_loss_pct,
    max_drawdown_pct: p.max_drawdown_pct,
    max_daily_loss_pct: p.max_daily_loss_pct,
    max_risk_per_trade_pct: p.max_risk_per_trade_pct,
    max_total_exposure_pct: p.max_total_exposure_pct,
    max_simultaneous_orders: p.max_simultaneous_orders,
  };
}