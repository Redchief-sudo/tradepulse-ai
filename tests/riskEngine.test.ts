import { describe, it, expect } from 'vitest';
import { evaluateRisk, riskLimitsForProfile } from '../base44/shared/riskEngine.ts';

// Critical invariant: a DENIED risk result means ZERO shares — never "at least
// one share". This is the #1 safety property of the risk engine.
describe('evaluateRisk — denied means zero shares', () => {
  const limits = riskLimitsForProfile('balanced');
  const baseSnapshot = {
    holdings: [],
    totalEquity: 10000,
    sectorMap: {},
    openPositions: 0,
    tradesToday: 0,
    dailyPnlPct: 0,
    totalExposure: 0,
    totalExposurePct: 0,
    outstandingOrders: 0,
  };

  it('returns approvedQuantity 0 when confidence is below minimum', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 50, stop_loss: 140,
    };
    const result = evaluateRisk(intent, baseSnapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
  });

  it('returns approvedQuantity 0 when max daily trades reached', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const snapshot = { ...baseSnapshot, tradesToday: limits.max_daily_trades };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
  });

  it('returns approvedQuantity 0 when max open positions reached', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const snapshot = { ...baseSnapshot, openPositions: limits.max_open_positions };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
  });

  it('returns approvedQuantity 0 when daily loss exceeded', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const snapshot = { ...baseSnapshot, dailyPnlPct: -limits.max_daily_loss_pct - 0.1 };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
  });

  it('returns approvedQuantity 0 when kill switch is active', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const result = evaluateRisk(intent, baseSnapshot, limits, { killSwitch: true });
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
    expect(result.reasons).toContain('KILL_SWITCH_ACTIVE');
  });

  it('returns approvedQuantity 0 when total exposure exceeded', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 100, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const snapshot = { ...baseSnapshot, totalExposurePct: limits.max_total_exposure_pct, totalExposure: 4000 };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
  });

  it('returns approvedQuantity 0 when spread exceeds limit', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const result = evaluateRisk(intent, baseSnapshot, limits, { bid: 148, ask: 155 });
    const spreadPct = ((155 - 148) / 151.5) * 100;
    expect(result.approved).toBe(false);
    expect(result.reasons.some((r) => r.includes('SPREAD_EXCEEDS_LIMIT'))).toBe(true);
    expect(spreadPct).toBeGreaterThan(limits.spread_limit_pct);
  });

  it('returns approvedQuantity 0 when slippage exceeds limit', () => {
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const result = evaluateRisk(intent, baseSnapshot, limits, { estimated_slippage_pct: limits.slippage_limit_pct + 0.5 });
    expect(result.approved).toBe(false);
    expect(result.reasons.some((r) => r.includes('SLIPPAGE_EXCEEDS_LIMIT'))).toBe(true);
  });
});

describe('evaluateRisk — sell validation', () => {
  const limits = riskLimitsForProfile('balanced');
  const snapshot = {
    holdings: [{ symbol: 'AAPL', shares: 50 }],
    totalEquity: 10000,
    sectorMap: {},
    openPositions: 1,
    tradesToday: 0,
    dailyPnlPct: 0,
    totalExposure: 0,
    totalExposurePct: 0,
    outstandingOrders: 0,
  };

  it('rejects selling more shares than held', () => {
    const intent = {
      symbol: 'AAPL', side: 'sell', requested_quantity: 100, limit_price: 150, price: 150,
    };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(false);
    expect(result.approvedQuantity).toBe(0);
    expect(result.reasons[0]).toContain('INSUFFICIENT_POSITION_TO_SELL');
  });

  it('approves selling shares that are held', () => {
    const intent = {
      symbol: 'AAPL', side: 'sell', requested_quantity: 30, limit_price: 150, price: 150,
    };
    const result = evaluateRisk(intent, snapshot, limits);
    expect(result.approved).toBe(true);
    expect(result.approvedQuantity).toBe(30);
  });
});

describe('evaluateRisk — protective short cover', () => {
  it('allows a classified buy-to-cover through a kill switch without applying new-exposure caps', () => {
    const limits = riskLimitsForProfile('balanced');
    const snapshot = {
      holdings: [{ symbol: 'MSFT', shares: -13 }], totalEquity: 10000,
      sectorMap: { Technology: 6500 }, openPositions: limits.max_open_positions,
      tradesToday: limits.max_daily_trades, dailyPnlPct: -10,
      totalExposure: 6500, totalExposurePct: 65, outstandingOrders: limits.max_simultaneous_orders,
    };
    const result = evaluateRisk(
      { symbol: 'MSFT', side: 'buy', requested_quantity: 13, price: 500, limit_price: 500, sector: 'Technology' },
      snapshot,
      limits,
      { protectiveExit: true, killSwitch: true, bid: 499.9, ask: 500, estimated_slippage_pct: 0.01 }
    );
    expect(result).toMatchObject({ approved: true, approvedQuantity: 13, reasons: ['PROTECTIVE_EXIT'] });
  });
});

describe('evaluateRisk — position sizing caps', () => {
  const limits = riskLimitsForProfile('balanced');
  const snapshot = {
    holdings: [],
    totalEquity: 10000,
    sectorMap: {},
    openPositions: 0,
    tradesToday: 0,
    dailyPnlPct: 0,
    totalExposure: 0,
    totalExposurePct: 0,
    outstandingOrders: 0,
  };

  it('caps position by max_position_pct', () => {
    // Request 10 shares ($1500) — above max_position ($700) but below max_exposure ($4000)
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const result = evaluateRisk(intent, snapshot, limits, { bid: 149.5, ask: 150.5, estimated_slippage_pct: 0.5 });
    expect(result.approved).toBe(true);
    const maxNotional = (limits.max_position_pct / 100) * 10000;
    expect(result.approvedQuantity * 150).toBeLessThanOrEqual(maxNotional + 0.01);
  });

  it('caps position by risk-based sizing (risk budget / stop distance)', () => {
    // Request 10 shares with a tight stop — risk-based qty would be 30 shares
    // but max_position_pct caps it to ~4.67 shares
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 149, // very tight stop = high qty
    };
    const result = evaluateRisk(intent, snapshot, limits, { bid: 149.5, ask: 150.5, estimated_slippage_pct: 0.5 });
    expect(result.approved).toBe(true);
    const maxNotional = (limits.max_position_pct / 100) * 10000;
    expect(result.approvedQuantity * 150).toBeLessThanOrEqual(maxNotional + 0.1);
  });

  it('caps position by sector concentration', () => {
    // Sector already at $1800 (18% of 10k), limit is 20% = $2000, remaining = $200
    const sectorSnapshot = {
      ...snapshot,
      sectorMap: { Technology: 1800 },
      totalExposure: 1800,
      totalExposurePct: 18,
    };
    // Request 5 shares ($750) — exposure 18+7.5=25.5% < 40%, passes exposure check
    const intent = {
      symbol: 'AAPL', side: 'buy', requested_quantity: 5, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const result = evaluateRisk(intent, sectorSnapshot, limits, { bid: 149.5, ask: 150.5, estimated_slippage_pct: 0.5 });
    expect(result.approved).toBe(true);
    // Remaining sector capacity = 2000 - 1800 = 200 → 200/150 ≈ 1.33 shares
    expect(result.approvedQuantity).toBeLessThanOrEqual(2);
  });
});

describe('riskLimitsForProfile', () => {
  it('returns balanced limits for unknown profiles', () => {
    const limits = riskLimitsForProfile('unknown_profile');
    expect(limits).toBe(riskLimitsForProfile('balanced'));
  });

  it('returns correct limits for each known profile', () => {
    expect(riskLimitsForProfile('aggressive').max_position_pct).toBe(15);
    expect(riskLimitsForProfile('balanced').max_position_pct).toBe(7);
    expect(riskLimitsForProfile('conservative').max_position_pct).toBe(5);
    expect(riskLimitsForProfile('micro').max_position_pct).toBe(20);
  });

  it('all profiles have spread and slippage limits defined', () => {
    const profiles = ['aggressive', 'balanced', 'conservative', 'micro'];
    for (const p of profiles) {
      const l = riskLimitsForProfile(p);
      expect(l.spread_limit_pct).toBeGreaterThan(0);
      expect(l.slippage_limit_pct).toBeGreaterThan(0);
      expect(l.max_drawdown_pct).toBeGreaterThan(0);
      expect(l.max_total_exposure_pct).toBeGreaterThan(0);
      expect(l.max_simultaneous_orders).toBeGreaterThan(0);
    }
  });
});

// Rev.14 audit fixes: slippage fail-closed and max drawdown in canonical risk engine.
describe('evaluateRisk — Rev.14 fail-closed checks', () => {
  const limits = riskLimitsForProfile('balanced');
  const goodSnapshot = {
    holdings: [], totalEquity: 10000, sectorMap: {}, openPositions: 0,
    tradesToday: 0, dailyPnlPct: 0, totalExposure: 0, totalExposurePct: 0,
    outstandingOrders: 0,
  };
  const goodIntent = {
    symbol: 'AAPL', side: 'buy', requested_quantity: 10, limit_price: 150, price: 150,
    sector: 'Technology', confidence: 90, stop_loss: 140,
  };

  it('rejects buys when slippage estimate is missing (fail-closed)', () => {
    const result = evaluateRisk(goodIntent, goodSnapshot, limits, {
      killSwitch: false,
      skipMarketDataChecks: false,
      // no estimated_slippage_pct — should fail closed
    });
    expect(result.approved).toBe(false);
    expect(result.reasons).toContain('NO_SLIPPAGE_ESTIMATE');
    expect(result.approvedQuantity).toBe(0);
  });

  it('rejects buys when slippage exceeds limit', () => {
    const result = evaluateRisk(goodIntent, goodSnapshot, limits, {
      killSwitch: false,
      skipMarketDataChecks: false,
      estimated_slippage_pct: 5.0,
    });
    expect(result.approved).toBe(false);
    expect(result.reasons.some((r) => r.startsWith('SLIPPAGE_EXCEEDS_LIMIT'))).toBe(true);
    expect(result.approvedQuantity).toBe(0);
  });

  it('skips slippage and spread checks when skipMarketDataChecks is true', () => {
    const result = evaluateRisk(goodIntent, goodSnapshot, limits, {
      killSwitch: false,
      skipMarketDataChecks: true, // internal paper mode — no real market data
    });
    // Should not have NO_SLIPPAGE_ESTIMATE or NO_QUOTE_DATA reasons
    expect(result.reasons).not.toContain('NO_SLIPPAGE_ESTIMATE');
    expect(result.reasons).not.toContain('NO_QUOTE_DATA_FOR_SPREAD_CHECK');
  });

  it('rejects buys when max drawdown is breached (canonical engine)', () => {
    const result = evaluateRisk(goodIntent, goodSnapshot, limits, {
      killSwitch: false,
      skipMarketDataChecks: true,
      maxDrawdownBreached: true,
    });
    expect(result.approved).toBe(false);
    expect(result.reasons).toContain('MAX_DRAWDOWN_BREACHED');
    expect(result.approvedQuantity).toBe(0);
  });

  it('allows sells even when max drawdown is breached', () => {
    const sellIntent = {
      symbol: 'AAPL', side: 'sell', requested_quantity: 10, limit_price: 150, price: 150,
      sector: 'Technology', confidence: 90, stop_loss: 140,
    };
    const snapshot = {
      ...goodSnapshot,
      holdings: [{ symbol: 'AAPL', shares: 10 }],
    };
    const result = evaluateRisk(sellIntent, snapshot, limits, {
      killSwitch: false,
      skipMarketDataChecks: true,
      maxDrawdownBreached: true,
    });
    expect(result.approved).toBe(true);
    expect(result.approvedQuantity).toBe(10);
  });
});
