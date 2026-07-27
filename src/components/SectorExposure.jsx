import React, { useMemo } from 'react';
import { motion } from 'framer-motion';
import { ShieldAlert } from 'lucide-react';

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

const SECTOR_COLORS = {
  Technology: '#3b82f6',
  Healthcare: '#10b981',
  Finance: '#8b5cf6',
  Financial: '#8b5cf6',
  Consumer: '#f59e0b',
  Energy: '#ef4444',
  Industrial: '#06b6d4',
  Automotive: '#ec4899',
  Real: '#84cc16',
  Materials: '#f97316',
  Utilities: '#0ea5e9',
  Communication: '#a855f7',
  Other: '#64748b',
};

const CONCENTRATION_THRESHOLD = 40;

function colorFor(sector) {
  return SECTOR_COLORS[sector] || SECTOR_COLORS.Other;
}

export default function SectorExposure({ holdings }) {
  const sectorData = useMemo(() => {
    const sectors = {};
    holdings.forEach((h) => {
      const sector = h.sector || 'Other';
      const value = h.shares * (h.current_price || h.avg_price);
      sectors[sector] = (sectors[sector] || 0) + value;
    });
    const total = Object.values(sectors).reduce((s, v) => s + v, 0);
    return Object.entries(sectors)
      .map(([sector, value]) => ({
        sector,
        value,
        percent: total > 0 ? (value / total) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value);
  }, [holdings]);

  if (sectorData.length === 0) return null;

  const overConcentrated = sectorData.filter((s) => s.percent >= CONCENTRATION_THRESHOLD);
  const maxPercent = Math.max(...sectorData.map((s) => s.percent));

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-border flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <ShieldAlert className="w-4 h-4 text-accent" />
          <h3 className="font-semibold">Portfolio Risk Analysis</h3>
        </div>
        {overConcentrated.length > 0 ? (
          <span className="flex items-center gap-1.5 text-xs text-amber-400 font-medium">
            <ShieldAlert className="w-3.5 h-3.5" />
            High concentration in {overConcentrated.length} sector
            {overConcentrated.length > 1 ? 's' : ''}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">Well diversified</span>
        )}
      </div>

      <div className="p-5 space-y-4">
        {sectorData.map((s, i) => {
          const color = colorFor(s.sector);
          const isOver = s.percent >= CONCENTRATION_THRESHOLD;
          return (
            <div key={s.sector}>
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-2">
                  <span
                    className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                    style={{ backgroundColor: isOver ? '#f59e0b' : color }}
                  />
                  <span className="text-sm font-medium">{s.sector}</span>
                  {isOver && (
                    <span className="text-xs text-amber-400 font-medium hidden sm:inline">
                      High exposure
                    </span>
                  )}
                </div>
                <div className="text-sm">
                  <span className="font-semibold">{formatCurrency(s.value)}</span>
                  <span className="text-muted-foreground ml-2">{s.percent.toFixed(1)}%</span>
                </div>
              </div>
              <div className="h-2 rounded-full bg-muted overflow-hidden">
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${s.percent}%` }}
                  transition={{ duration: 0.5, delay: i * 0.05 }}
                  className="h-full rounded-full"
                  style={{ backgroundColor: isOver ? '#f59e0b' : color }}
                />
              </div>
            </div>
          );
        })}

        <div className="pt-2 border-t border-border/50 text-xs text-muted-foreground leading-relaxed">
          {overConcentrated.length > 0
            ? `Sectors over ${CONCENTRATION_THRESHOLD}% exposure are flagged as high concentration. Consider rebalancing to reduce risk.`
            : `All sectors are below the ${CONCENTRATION_THRESHOLD}% concentration threshold.`}
        </div>
      </div>
    </motion.div>
  );
}