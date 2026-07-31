// Transaction cost model — deterministic per-asset-class estimates of commission,
// slippage, and spread. Used to compute the net edge of a trade (expected return
// minus round-trip costs) so the engine never executes a trade whose gross edge
// is smaller than its cost to put on.

const COST_MODEL = {
  stocks: { commission_bps: 0, slippage_bps: 5, spread_bps: 2 },      // $0 retail commission, ~5bps slippage
  crypto: { commission_bps: 10, slippage_bps: 15, spread_bps: 8 },
  forex: { commission_bps: 2, slippage_bps: 1, spread_bps: 2 },
  commodities: { commission_bps: 5, slippage_bps: 8, spread_bps: 4 },
  fixed_income: { commission_bps: 3, slippage_bps: 4, spread_bps: 6 },
};

export function costModelFor(assetClass) {
  return COST_MODEL[assetClass] || COST_MODEL.stocks;
}

// estimateCosts — one-way and round-trip cost of a position of the given notional.
export function estimateCosts(assetClass, notional) {
  const m = costModelFor(assetClass);
  const oneWayBps = m.commission_bps + m.slippage_bps + m.spread_bps;
  const roundTripBps = oneWayBps * 2;
  return {
    commission_bps: m.commission_bps,
    slippage_bps: m.slippage_bps,
    spread_bps: m.spread_bps,
    one_way_bps: oneWayBps,
    round_trip_bps: roundTripBps,
    one_way_cost: (oneWayBps / 10000) * notional,
    round_trip_cost: (roundTripBps / 10000) * notional,
    round_trip_cost_pct: roundTripBps / 100,
  };
}

// netEdge — expected return after round-trip costs (in percentage points).
// grossReturnPct is the strategy's expected return, e.g. (target - entry)/entry * 100.
// net <= 0 ⇒ the trade costs more to put on than it's expected to make.
export function netEdge(grossReturnPct, assetClass) {
  const m = costModelFor(assetClass);
  const roundTripCostPct = ((m.commission_bps + m.slippage_bps + m.spread_bps) * 2) / 100;
  return grossReturnPct - roundTripCostPct;
}