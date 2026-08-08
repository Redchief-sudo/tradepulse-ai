// Outcome measurement — closes the feedback loop by labeling each AI buy decision
// with realized/forward returns, benchmark-relative return, and excursion metrics,
// computed from the immutable Fill ledger + the PriceSnapshot time series.
// USER-SCOPED: all queries filter by user_id.

const WINDOWS = [
  { field: 'return_5m', ms: 5 * 60 * 1000, toleranceMs: 2 * 60 * 1000 },
  { field: 'return_1h', ms: 60 * 60 * 1000, toleranceMs: 15 * 60 * 1000 },
  { field: 'return_1d', ms: 24 * 60 * 60 * 1000, toleranceMs: 2 * 60 * 60 * 1000 },
  { field: 'return_5d', ms: 5 * 24 * 60 * 60 * 1000, toleranceMs: 6 * 60 * 60 * 1000 },
];

function snapshotTime(snapshot) { return new Date(snapshot.provider_timestamp || snapshot.timestamp).getTime(); }

function nearestAtOrAfter(snaps, targetTs, toleranceMs) {
  let best = null; let bestDiff = Infinity;
  for (const s of snaps) {
    const t = snapshotTime(s);
    if (t < targetTs) continue;
    const diff = t - targetTs;
    if (diff < bestDiff) { bestDiff = diff; best = s; }
  }
  return best && bestDiff <= toleranceMs ? best : null;
}

function nearestOverall(snaps, targetTs, toleranceMs = Infinity) {
  let best = null; let bestDiff = Infinity;
  for (const s of snaps) {
    const diff = Math.abs(snapshotTime(s) - targetTs);
    if (diff < bestDiff) { bestDiff = diff; best = s; }
  }
  return best && bestDiff <= toleranceMs ? best : null;
}

export function deriveDecisionOutcome(decision, lots, fills) {
  const entryFills = fills.filter((fill) => decision.trade_intent_id
    ? fill.trade_intent_id === decision.trade_intent_id && fill.side === 'buy'
    : fill.fill_id === decision.settlement_event_id);
  if (!entryFills.length) return { lineage_complete: false, outcome_status: 'open' };
  const entryIds = new Set(entryFills.map((fill) => fill.fill_id));
  const decisionLots = lots.filter((lot) => entryIds.has(lot.originating_fill_id));
  if (decisionLots.length !== entryFills.length || decisionLots.some((lot) => lot.provenance_quality !== 'verified')) {
    return { lineage_complete: false, outcome_status: 'open' };
  }
  const fillById = new Map(fills.map((fill) => [fill.fill_id, fill]));
  const resolved = [];
  for (const lot of decisionLots) {
    let allocations;
    try { allocations = JSON.parse(lot.closure_fill_ids || '[]'); } catch (error) { return { lineage_complete: false, outcome_status: 'open' }; }
    for (const allocation of allocations) resolved.push({ lot, allocation, fill: fillById.get(allocation.fill_id) });
  }
  if (resolved.some(({ fill }) => !fill)) return { lineage_complete: false, outcome_status: 'open' };
  const closedQty = resolved.reduce((sum, item) => sum + (Number(item.allocation.qty) || 0), 0);
  const openedQty = decisionLots.reduce((sum, lot) => sum + (Number(lot.quantity_opened) || 0), 0);
  const fullyClosed = openedQty > 0 && closedQty >= openedQty - 0.0001 && decisionLots.every((lot) => Number(lot.quantity_remaining || 0) <= 0.0001);
  const entryTimes = decisionLots.map((lot) => new Date(lot.acquisition_timestamp).getTime()).filter(Number.isFinite);
  const entryAt = entryTimes.length ? Math.min(...entryTimes) : new Date(decision.created_date).getTime();
  if (!fullyClosed) {
    return { lineage_complete: true, outcome_status: 'open', outcome_quantity: closedQty, entry_fill_id: entryFills[0].fill_id, entry_fill_ids: entryFills.map((fill) => fill.fill_id), entry_at: entryAt };
  }
  const entryCosts = entryFills.reduce((sum, fill) => sum + Number(fill.commission || 0) + Number(fill.fees || 0), 0);
  const exitCosts = resolved.reduce((sum, item) => {
    const fillQty = Number(item.fill.filled_quantity) || 0;
    const allocatedFraction = fillQty > 0 ? (Number(item.allocation.qty) || 0) / fillQty : 0;
    return sum + (Number(item.fill.commission || 0) + Number(item.fill.fees || 0)) * allocatedFraction;
  }, 0);
  const entryCost = decisionLots.reduce((sum, lot) => sum + Number(lot.quantity_opened || 0) * Number(lot.acquisition_price || 0), 0) + entryCosts;
  const exitValue = resolved.reduce((sum, item) => sum + (Number(item.allocation.qty) || 0) * Number(item.fill.filled_price || 0), 0) - exitCosts;
  const exitTimes = resolved.map((item) => new Date(item.fill.timestamp).getTime()).filter(Number.isFinite);
  const finalExitAt = exitTimes.length ? Math.max(...exitTimes) : null;
  return {
    lineage_complete: true,
    outcome_status: 'realized',
    realized_return: entryCost > 0 ? (exitValue - entryCost) / entryCost : null,
    holding_period_minutes: finalExitAt && Number.isFinite(entryAt) ? (finalExitAt - entryAt) / 60000 : null,
    outcome_quantity: openedQty,
    entry_fill_id: entryFills[0].fill_id,
    entry_fill_ids: entryFills.map((fill) => fill.fill_id),
    exit_fill_ids: [...new Set(resolved.map((item) => item.allocation.fill_id))],
    entry_at: entryAt,
    final_exit_at: finalExitAt,
  };
}

export const deriveLotOutcome = deriveDecisionOutcome;

// labelOutcomes — label every executed BUY decision with forward/realized returns.
// USER-SCOPED: filters by userId to prevent cross-user data leakage.
export async function labelOutcomes(sr, userId) {
  const decisions = await sr.entities.AITradeDecision.filter({ user_id: userId }, '-created_date', 200);
  const buyDecisions = decisions.filter((d) => d.action === 'buy' && d.status === 'executed' && !(d.outcome_status === 'realized' && d.lineage_complete));
  if (!buyDecisions.length) return { labeled: 0 };

  const fills = await sr.entities.Fill.filter({ user_id: userId }, '-created_date', 5000);
  const lots = await sr.entities.PositionLot.filter({ user_id: userId }, '-created_date', 5000);
  const settlementEvents = await sr.entities.SettlementEvent.filter({ user_id: userId }, '-created_date', 5000);
  const intentByEventId = new Map(settlementEvents.map((event) => [event.event_id, event.trade_intent_id]));
  const allSnaps = await sr.entities.PriceSnapshot.filter({ user_id: userId }, '-timestamp', 5000);
  const snapsBySym = {};
  allSnaps.forEach((s) => {
    const k = String(s.symbol).toUpperCase();
    (snapsBySym[k] = snapsBySym[k] || []).push(s);
  });

  let labeled = 0;
  for (const d of buyDecisions) {
    const effectiveDecision = { ...d, trade_intent_id: d.trade_intent_id || intentByEventId.get(d.settlement_event_id) };
    const sym = String(d.symbol).toUpperCase();
    const entryPrice = d.price;
    const lineage = deriveDecisionOutcome(effectiveDecision, lots, fills);
    const entryTs = lineage.entry_at || new Date(d.created_date).getTime();
    let outcomeStatus = lineage.outcome_status;
    let realizedReturn = lineage.realized_return ?? null;
    let holdingPeriodMin = lineage.holding_period_minutes ?? null;

    const symSnaps = snapsBySym[sym] || [];
    const spySnaps = snapsBySym['SPY'] || [];
    const windowReturns = {};
    let mae = null, mfe = null, benchmarkReturn = null;

    if (symSnaps.length && entryPrice > 0) {
      for (const w of WINDOWS) {
        const s = nearestAtOrAfter(symSnaps, entryTs + w.ms, w.toleranceMs);
        windowReturns[w.field] = s ? (s.price - entryPrice) / entryPrice : null;
      }
      const excursionEnd = lineage.final_exit_at || Date.now();
      const since = symSnaps.filter((s) => snapshotTime(s) >= entryTs && snapshotTime(s) <= excursionEnd).map((s) => s.price);
      if (since.length) {
        mae = (Math.min(...since) - entryPrice) / entryPrice;
        mfe = (Math.max(...since) - entryPrice) / entryPrice;
      }
      const benchmarkEnd = lineage.final_exit_at || Date.now();
      const spyEntry = nearestOverall(spySnaps, entryTs, 60 * 60 * 1000);
      const spyExit = nearestOverall(spySnaps, benchmarkEnd, 60 * 60 * 1000);
      if (spyEntry && spyExit && spyEntry.price > 0) {
        benchmarkReturn = (spyExit.price - spyEntry.price) / spyEntry.price;
      }
    }

    if (outcomeStatus === 'open' && holdingPeriodMin === null) {
      holdingPeriodMin = (Date.now() - entryTs) / 60000;
    }

    await sr.entities.AITradeDecision.update(d.id, {
      trade_intent_id: effectiveDecision.trade_intent_id || null,
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
      entry_fill_ids: lineage.entry_fill_ids ? JSON.stringify(lineage.entry_fill_ids) : null,
      exit_fill_ids: lineage.exit_fill_ids ? JSON.stringify(lineage.exit_fill_ids) : null,
      final_exit_at: lineage.final_exit_at ? new Date(lineage.final_exit_at).toISOString() : null,
      labeled_at: new Date().toISOString(),
    });
    labeled++;
  }
  return { labeled };
}
