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

// labelOutcomes — label every executed BUY decision with forward/realized returns.
// USER-SCOPED: filters by userId to prevent cross-user data leakage.
export async function labelOutcomes(sr, userId) {
  const decisions = await sr.entities.AITradeDecision.filter({ user_id: userId }, '-created_date', 200);
  const buyDecisions = decisions.filter((d) => d.action === 'buy' && d.status === 'executed');
  if (!buyDecisions.length) return { labeled: 0 };

  const fills = await sr.entities.Fill.filter({ user_id: userId }, '-created_date', 5000);
  const allSnaps = await sr.entities.PriceSnapshot.list('-timestamp', 5000);
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

    // Realized? find a sell fill for this symbol after entry.
    const sellFills = fills.filter(
      (f) => String(f.symbol).toUpperCase() === sym && f.side === 'sell' && new Date(f.timestamp).getTime() >= entryTs
    );
    let outcomeStatus = 'open';
    let realizedReturn = null;
    let holdingPeriodMin = null;
    if (sellFills.length) {
      const exit = sellFills[0];
      realizedReturn = entryPrice > 0 ? (exit.filled_price - entryPrice) / entryPrice : null;
      holdingPeriodMin = (new Date(exit.timestamp).getTime() - entryTs) / 60000;
      outcomeStatus = 'realized';
    }

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
      labeled_at: new Date().toISOString(),
    });
    labeled++;
  }
  return { labeled };
}