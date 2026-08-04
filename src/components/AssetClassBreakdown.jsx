import React from 'react';
import { motion } from 'framer-motion';
import { TrendingUp, Bitcoin, DollarSign, Gem, Landmark, Wallet } from 'lucide-react';

const ASSET_CLASSES = [
  { key: 'stocks', label: 'Stocks', icon: TrendingUp, color: '#10b981' },
  { key: 'crypto', label: 'Crypto', icon: Bitcoin, color: '#f59e0b' },
  { key: 'forex', label: 'Forex', icon: DollarSign, color: '#3b82f6' },
  { key: 'commodities', label: 'Commodities', icon: Gem, color: '#8b5cf6' },
  { key: 'fixed_income', label: 'Fixed Income', icon: Landmark, color: '#06b6d4' },
];

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

export default function AssetClassBreakdown({ holdings }) {
  const byClass = {};
  let totalValue = 0;
  let totalInvested = 0;
  let totalDayPL = 0;

  for (const h of holdings) {
    const ac = h.asset_class || 'stocks';
    if (!byClass[ac]) byClass[ac] = { value: 0, invested: 0, dayPL: 0, count: 0 };
    const value = h.shares * (h.current_price || h.avg_price);
    const invested = h.shares * h.avg_price;
    const dayPL = value * ((h.day_change_percent || 0) / 100);
    byClass[ac].value += value;
    byClass[ac].invested += invested;
    byClass[ac].dayPL += dayPL;
    byClass[ac].count += 1;
    totalValue += value;
    totalInvested += invested;
    totalDayPL += dayPL;
  }

  const rows = ASSET_CLASSES.filter((ac) => byClass[ac.key]);
  if (rows.length === 0) return null;

  const totalPL = totalValue - totalInvested;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-border">
        <h3 className="font-semibold flex items-center gap-2">
          <Wallet className="w-4 h-4 text-primary" />
          Asset Class Breakdown
        </h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground text-xs border-b border-border">
              <th className="text-left font-medium p-4">Asset Class</th>
              <th className="text-right font-medium p-4">Positions</th>
              <th className="text-right font-medium p-4">Value</th>
              <th className="text-right font-medium p-4 hidden sm:table-cell">Alloc %</th>
              <th className="text-right font-medium p-4">P&L</th>
              <th className="text-right font-medium p-4 hidden sm:table-cell">Day Change</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ac) => {
              const d = byClass[ac.key];
              const pl = d.value - d.invested;
              const plPct = d.invested > 0 ? (pl / d.invested) * 100 : 0;
              const alloc = totalValue > 0 ? (d.value / totalValue) * 100 : 0;
              const Icon = ac.icon;
              return (
                <tr key={ac.key} className="border-b border-border/50 hover:bg-muted/30 transition-colors">
                  <td className="p-4">
                    <div className="flex items-center gap-2">
                      <div
                        className="w-8 h-8 rounded-lg flex items-center justify-center"
                        style={{ backgroundColor: `${ac.color}15` }}
                      >
                        <Icon className="w-4 h-4" style={{ color: ac.color }} />
                      </div>
                      <span className="font-semibold">{ac.label}</span>
                    </div>
                  </td>
                  <td className="text-right p-4 text-muted-foreground">{d.count}</td>
                  <td className="text-right p-4 font-medium">{formatCurrency(d.value)}</td>
                  <td className="text-right p-4 hidden sm:table-cell">
                    <div className="flex items-center justify-end gap-2">
                      <div className="w-16 h-1.5 rounded-full bg-muted overflow-hidden">
                        <div
                          className="h-full rounded-full"
                          style={{ width: `${alloc}%`, backgroundColor: ac.color }}
                        />
                      </div>
                      <span className="text-xs">{alloc.toFixed(1)}%</span>
                    </div>
                  </td>
                  <td className="text-right p-4">
                    <span className={pl >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                      {pl >= 0 ? '+' : ''}
                      {formatCurrency(pl)}
                      <span className="text-xs block opacity-80">
                        ({plPct >= 0 ? '+' : ''}
                        {plPct.toFixed(2)}%)
                      </span>
                    </span>
                  </td>
                  <td className="text-right p-4 hidden sm:table-cell">
                    <span className={d.dayPL >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                      {d.dayPL >= 0 ? '+' : ''}
                      {formatCurrency(d.dayPL)}
                    </span>
                  </td>
                </tr>
              );
            })}
            <tr className="border-t-2 border-border">
              <td className="p-4 font-semibold">Total</td>
              <td className="text-right p-4 font-semibold text-muted-foreground">{holdings.length}</td>
              <td className="text-right p-4 font-semibold">{formatCurrency(totalValue)}</td>
              <td className="text-right p-4 hidden sm:table-cell font-semibold">100%</td>
              <td className="text-right p-4 font-semibold">
                <span className={totalPL >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                  {totalPL >= 0 ? '+' : ''}
                  {formatCurrency(totalPL)}
                </span>
              </td>
              <td className="text-right p-4 hidden sm:table-cell font-semibold">
                <span className={totalDayPL >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                  {totalDayPL >= 0 ? '+' : ''}
                  {formatCurrency(totalDayPL)}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}