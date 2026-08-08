export function calculatePositionDayPnl(holdings) {
  return holdings.reduce((sum, holding) => {
    const currentValue = Number(holding.shares || 0) * Number(holding.current_price || holding.avg_price || 0);
    const changeFraction = Number(holding.day_change_percent || 0) / 100;
    const previousValue = changeFraction > -1 ? currentValue / (1 + changeFraction) : currentValue;
    return sum + currentValue - previousValue;
  }, 0);
}

export function indexClosedLotsByFillId(positionLots) {
  const result = {};
  const malformedLotIds = [];
  for (const lot of positionLots) {
    if (!['closed', 'partially_closed'].includes(lot.status)) continue;
    let allocations;
    try { allocations = JSON.parse(lot.closure_fill_ids || '[]'); }
    catch (_error) { malformedLotIds.push(lot.id || 'unknown'); continue; }
    if (!Array.isArray(allocations)) { malformedLotIds.push(lot.id || 'unknown'); continue; }
    for (const allocation of allocations) {
      const fillId = typeof allocation === 'string' ? allocation : allocation?.fill_id;
      if (!fillId) continue;
      if (!result[fillId]) result[fillId] = [];
      result[fillId].push(lot);
    }
  }
  return { index: result, malformedLotIds };
}

export function calculateDailyReturn(startingEquity, endingEquity) {
  if (startingEquity == null || endingEquity == null || startingEquity <= 0) return null;
  return ((endingEquity - startingEquity) / startingEquity) * 100;
}
