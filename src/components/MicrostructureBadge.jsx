import React from 'react';
import { Waves } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function MicrostructureBadge({ signal }) {
  if (!signal) return null;
  const isBullish = /accumulation|imbalance|pressure|inflow|block print/i.test(signal);
  const isBearish = /distribution|outflow|sell|dump/i.test(signal);
  return (
    <span
      className={cn(
        'text-xs px-2 py-0.5 rounded-full border flex items-center gap-1',
        isBullish
          ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
          : isBearish
          ? 'bg-red-500/10 text-red-400 border-red-500/30'
          : 'bg-accent/10 text-accent border-accent/30'
      )}
    >
      <Waves className="w-3 h-3" />
      {signal}
    </span>
  );
}