import React from 'react';
import { motion } from 'framer-motion';
import { TRADE_PROFILES } from '@/lib/tradeProfiles';
import { cn } from '@/lib/utils';
import { Gauge } from 'lucide-react';

const RISK_BADGE = {
  High: 'bg-red-500/10 text-red-400 border-red-500/30',
  Medium: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  Low: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
};

export default function TradingProfileSelector({ value, onChange }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-6 space-y-4"
    >
      <div className="flex items-center gap-2">
        <Gauge className="w-4 h-4 text-accent" />
        <h2 className="font-semibold">AI Trading Profile</h2>
      </div>
      <p className="text-xs text-muted-foreground -mt-2 leading-relaxed">
        Choose a risk profile. The AI applies institutional-grade risk limits — max position size,
        sector concentration, minimum confidence, and daily trade caps — to every scan and execution.
      </p>
      <div className="grid grid-cols-1 gap-2">
        {Object.values(TRADE_PROFILES).map((p) => (
          <button
            key={p.id}
            onClick={() => onChange(p.id)}
            className={cn(
              'text-left rounded-xl border p-4 transition-all',
              value === p.id
                ? 'border-primary bg-primary/10'
                : 'border-border hover:border-primary/40'
            )}
          >
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-sm">{p.name}</span>
                <span
                  className={cn(
                    'text-xs px-2 py-0.5 rounded-full border',
                    RISK_BADGE[p.risk_level]
                  )}
                >
                  {p.risk_level} Risk
                </span>
              </div>
              <div className="flex gap-3 text-xs text-muted-foreground flex-wrap">
                <span>
                  Max Pos: <span className="text-foreground font-medium">{p.max_position_pct}%</span>
                </span>
                <span>
                  Max Sector: <span className="text-foreground font-medium">{p.max_sector_pct}%</span>
                </span>
                <span>
                  Min Conf: <span className="text-foreground font-medium">{p.min_confidence}%</span>
                </span>
                <span>
                  Max Trades: <span className="text-foreground font-medium">{p.max_daily_trades}</span>
                </span>
              </div>
            </div>
            <p className="text-xs text-muted-foreground mt-2 leading-relaxed">{p.description}</p>
          </button>
        ))}
      </div>
    </motion.div>
  );
}