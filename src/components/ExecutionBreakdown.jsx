import React from 'react';
import { Split } from 'lucide-react';
import { computeExecutionParams } from '@/lib/autonomousScan';

export default function ExecutionBreakdown({ proposal }) {
  const exec = computeExecutionParams(proposal);
  return (
    <div className="rounded-xl border border-border bg-muted/20 p-2.5">
      <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
        <Split className="w-3.5 h-3.5 text-accent" />
        <span className="text-xs font-semibold">Algorithmic Execution</span>
        <span className="text-xs text-muted-foreground">· {exec.algorithm}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <div>
          Slices: <span className="text-foreground font-medium">{exec.slice_count}</span>
        </div>
        <div>
          Venue: <span className="text-foreground font-medium">{exec.venue}</span>
        </div>
        <div>
          Latency: <span className="text-foreground font-medium">{exec.latency_sensitivity}</span>
        </div>
        <div>
          Est. Slippage:{' '}
          <span className="text-amber-400 font-medium">{exec.expected_slippage_pct.toFixed(2)}%</span>
        </div>
      </div>
    </div>
  );
}