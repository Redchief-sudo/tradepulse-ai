export function computeSectorExposure(holdings) {
  const sectors = {};
  let total = 0;
  holdings.forEach((h) => {
    const sector = h.sector || 'Other';
    const value = Math.abs(h.shares * (h.current_price || h.avg_price));
    sectors[sector] = (sectors[sector] || 0) + value;
    total += value;
  });
  return {
    sectors: Object.entries(sectors)
      .map(([sector, value]) => ({
        sector,
        value,
        percent: total > 0 ? (value / total) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value),
    total,
  };
}

export function computePortfolioValue(holdings) {
  return holdings.reduce((sum, h) => sum + Math.abs(h.shares * (h.current_price || h.avg_price)), 0);
}

export function computeCappedPositionSize(
  suggestedPct,
  price,
  portfolioValue,
  currentSectorValue,
  sectorMax = 0.4,
  positionMax = 0.25
) {
  const maxPositionValue = positionMax * portfolioValue;
  const remainingSectorCapacity = sectorMax * portfolioValue - currentSectorValue;
  const aiSuggestedValue = (suggestedPct / 100) * portfolioValue;
  const positionValue = Math.min(
    aiSuggestedValue,
    maxPositionValue,
    Math.max(0, remainingSectorCapacity)
  );
  const shares = price > 0 && positionValue >= price ? Math.floor(positionValue / price) : 0;
  return { shares, positionValue: shares * price };
}

export function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}
