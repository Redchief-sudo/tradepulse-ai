import React from 'react';
import { cn } from '@/lib/utils';

const config = {
  STRONG_BUY: { label: 'Strong Buy', className: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30' },
  BUY: { label: 'Buy', className: 'bg-green-500/15 text-green-400 border-green-500/30' },
  HOLD: { label: 'Hold', className: 'bg-amber-500/15 text-amber-400 border-amber-500/30' },
  SELL: { label: 'Sell', className: 'bg-orange-500/15 text-orange-400 border-orange-500/30' },
  STRONG_SELL: { label: 'Strong Sell', className: 'bg-red-500/15 text-red-400 border-red-500/30' },
};

export default function RecommendationBadge({ recommendation }) {
  const c = config[recommendation] || config.HOLD;
  return (
    <span
      className={cn(
        'inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold border',
        c.className
      )}
    >
      {c.label}
    </span>
  );
}