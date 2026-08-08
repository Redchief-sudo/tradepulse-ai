// Outcome measurement — closes the feedback loop by labeling each AI buy decision
// with realized/forward returns, benchmark-relative return, and excursion metrics,
// computed from the immutable Fill ledger + the PriceSnapshot time series.
// USER-SCOPED: all queries filter by user_id.

const WINDOWS = [
  { field: 'return_5m', ms: 5 * 60 * 1000 },
  { field: 'return_1h', ms: 60 * 60 * 1000 },
  { field: 'return_1d', ms: 24 * 60 * 60 * 1000 },
  { field: 'return_5d', ms: 5 * 24 * 60 * 60 * 1000 },
];

function nearestAtOrAfter(snaps, targetTs) {
  let best = null; let bestDiff = Infinity;
  for (const s of snaps) {
    const t = new Date(s.timestamp).getTime();
    if (t < targetTs) continue;
    const diff = t - targetTs;
    if (diff < bestDiff) { bestDiff = diff; best = s; }
  }
  return best;
}

function nearestOverall(snaps, targetTs) {
  let best = null; let bestDiff = Infinity;
  for (const s of snaps) {
    const diff = Math.abs(new Date(s.timestamp).getTime() - targetTs);
    if (diff < bestDiff) { bestDiff = diff; best = s; }
  }
  return best;
}

export function deriveLotOutcome(decision, lots, fills) {
  const lot = lots.find((candidate) => candidate.originating_fill_id === decision.settlement_event_id);
  if (!lot || lot.provenance_quality !== 'verified') return { lineage_complete: false, outcome_status: 'open' };
  let allocations = [];
  try { allocations = JSON.parse(lot.closure_fill_ids || '[]'); } catch (error) { return { lineage_complete: false, outcome_status: 'open' }; }
  const fillById = new Map(fills.map((fill) => [fill.fill_id, fill]));
  const entryFill = fillById.get(decision.settlement_event_id);
  if (!entryFill) return { lineage_complete: false, outcome_status: 'open' };
  const resolved = allocations.map((allocation) => ({ allocation, fill: fillById.get(allocation.fill_id) }));
  if (resolved.some(({ fill }) => !fill)) return { lineage_complete: false, outcome_status: 'open' };
  const closedQty = resolved.reduce((sum, item) => sum + (Number(item.allocation.qty) || 0), 0);
  const openedQty = Number(lot.quantity_opened) || 0;
  const fullyClosed = openedQty > 0 && closedQty >= openedQty - 0.0001 && Number(lot.quantity_remaining || 0) <= 0.0001;
  if (!fullyClosed) {
    return { lineage_complete: true, outcome_status: 'open', outcome_quantity: closedQty, entry_fill_id: decision.settlement_event_id };
  }
  const entryCosts = Number(entryFill.commission || 0) + Number(entryFill.fees || 0);
  const exitCosts = resolved.reduce((sum, item) => sum + Number(item.fill.commission || 0) + Number(item.fill.fees || 0), 0);
  const entryCost = openedQty * Number(lot.acquisition_price || decision.price || 0) + entryCosts;
  const exitValue = resolved.reduce((sum, item) => sum + (Number(item.allocation.qty) || 0) * Number(item.fill.filled_price || 0), 0) - exitCosts;
  const exitTimes = resolved.map((item) => new Date(item.fill.timestamp).getTime()).filter(Number.isFinite);
  const entryTime = new Date(lot.acquisition_timestamp || decision.created_date).getTime();
  return {
    lineage_complete: true,
    outcome_status: 'realized',
    realized_return: entryCost > 0 ? (exitValue - entryCost) / entryCost : null,
    holding_period_minutes: exitTimes.length && Number.isFinite(entryTime) ? (Math.max(...exitTimes) - entryTime) / 60000 : null,
    outcome_quantity: openedQty,
    entry_fill_id: decision.settlement_event_id,
    exit_fill_ids: resolved.map((item) => item.allocation.fill_id),
  };
}

// labelOutcomes — label every executed BUY decision with forward/realized returns.
// USER-SCOPED: filters by userId to prevent cross-user data leakage.
export async function labelOutcomes(sr, userId) {
  const decisions = await sr.entities.AITradeDecision.filter({ user_id: userId }, '-created_date', 200);
  const buyDecisions = decisions.filter((d) => d.action === 'buy' && d.status === 'executed');
  if (!buyDecisions.length) return { labeled: 0 };

  const fills = await sr.entities.Fill.filter({ user_id: userId }, '-created_date', 5000);
  const lots = await sr.entities.PositionLot.filter({ user_id: userId }, '-created_date', 5000);
  const allSnaps = await sr.entities.PriceSnapshot.filter({ user_id: userId }, '-timestamp', 5000);
  const snapsBySym = {};
  allSnaps.forEach((s) => {
    const k = String(s.symbol).toUpperCase();
    (snapsBySym[k] = snapsBySym[k] || []).push(s);
  });

  let labeled = 0;
  for (const d of buyDecisions) {
    const sym = String(d.symbol).toUpperCase();
    const entryPrice = d.price;
    const entryTs = new Date(d.created_date).getTime();

    const lineage = deriveLotOutcome(d, lots, fills);
    let outcomeStatus = lineage.outcome_status;
    let realizedReturn = lineage.realized_return ?? null;
    let holdingPeriodMin = lineage.holding_period_minutes ?? null;

    const symSnaps = snapsBySym[sym] || [];
    const spySnaps = snapsBySym['SPY'] || [];
    const windowReturns = {};
    let mae = null, mfe = null, benchmarkReturn = null;

    if (symSnaps.length && entryPrice > 0) {
      for (const w of WINDOWS) {
        const s = nearestAtOrAfter(symSnaps, entryTs + w.ms);
        windowReturns[w.field] = s ? (s.price - entryPrice) / entryPrice : null;
      }
      const since = symSnaps.filter((s) => new Date(s.timestamp).getTime() >= entryTs).map((s) => s.price);
      if (since.length) {
        mae = (Math.min(...since) - entryPrice) / entryPrice;
        mfe = (Math.max(...since) - entryPrice) / entryPrice;
      }
      const spyEntry = nearestOverall(spySnaps, entryTs);
      const spyNow = spySnaps.length ? spySnaps[spySnaps.length - 1] : null;
      if (spyEntry && spyNow && spyEntry.price > 0) {
        benchmarkReturn = (spyNow.price - spyEntry.price) / spyEntry.price;
      }
    }

    if (outcomeStatus === 'open' && holdingPeriodMin === null) {
      holdingPeriodMin = (Date.now() - entryTs) / 60000;
    }

    await sr.entities.AITradeDecision.update(d.id, {
      return_5m: windowReturns.return_5m ?? null,
      return_1h: windowReturns.return_1h ?? null,
      return_1d: windowReturns.return_1d ?? null,
      return_5d: windowReturns.return_5d ?? null,
      realized_return: realizedReturn,
      benchmark_return: benchmarkReturn,
      max_adverse_excursion: mae,
      max_favorable_excursion: mfe,
      holding_period_minutes: holdingPeriodMin,
      outcome_status: outcomeStatus,
      lineage_complete: lineage.lineage_complete,
      outcome_quantity: lineage.outcome_quantity ?? null,
      entry_fill_id: lineage.entry_fill_id || null,
      exit_fill_ids: lineage.exit_fill_ids ? JSON.stringify(lineage.exit_fill_ids) : null,
      labeled_at: new Date().toISOString(),
    });
    labeled++;
  }
  return { labeled };
}
