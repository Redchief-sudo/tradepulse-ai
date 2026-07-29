import React from 'react';
import { motion } from 'framer-motion';
import { Activity, ShieldAlert } from 'lucide-react';
import { cn } from '@/lib/utils';

const REGIME_CONFIG = {
  low_vol_bull: { label: 'Low-Volatility Bull', color: 'emerald', strategy: 'Trend-following' },
  high_vol_bear: { label: 'High-Volatility Bear', color: 'red', strategy: 'Defensive / capital preservation' },
  range_bound_choppy: { label: 'Range-Bound Choppy', color: 'amber', strategy: 'Mean-reversion' },
  liquidity_crisis: { label: 'Liquidity Crisis', color: 'red', strategy: 'De-risk / capital preservation' },
  transition: { label: 'Regime Transition', color: 'violet', strategy: 'Adaptive / cautious' },
};

const COLOR_CLASS = {
  emerald: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
  amber: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  red: 'bg-red-500/10 text-red-400 border-red-500/30',
  violet: 'bg-violet-500/10 text-violet-400 border-violet-500/30',
};

export default function RegimeBanner({ regime, vetoedCount = 0 }) {
  if (!regime || !regime.market_regime) return null;
  const cfg = REGIME_CONFIG[regime.market_regime] || REGIME_CONFIG.transition;
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-4 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-accent" />
          <span className="font-semibold text-sm">Market Regime</span>
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full border',
              COLOR_CLASS[cfg.color]
            )}
          >
            {cfg.label}
          </span>
        </div>
        <div className="flex gap-4 text-xs text-muted-foreground flex-wrap">
          <span>
            Confidence:{' '}
            <span className="text-foreground font-medium">
              {(regime.regime_confidence || 0).toFixed(0)}%
            </span>
          </span>
          <span>
            Strategy: <span className="text-foreground font-medium">{cfg.strategy}</span>
          </span>
        </div>
      </div>
      {regime.regime_strategy && (
        <p className="text-xs text-muted-foreground mt-2 leading-relaxed">
          {regime.regime_strategy}
        </p>
      )}
      {vetoedCount > 0 && (
        <p className="text-xs text-red-400 mt-2 flex items-center gap-1.5">
          <ShieldAlert className="w-3.5 h-3.5" />
          Adversarial Risk Officer vetoed {vetoedCount} trade{vetoedCount > 1 ? 's' : ''} due to
          hidden risk.
        </p>
      )}
    </motion.div>
  );
}