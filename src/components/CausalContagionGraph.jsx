import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { GitBranch, Loader2, Play, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { runCausalContagion } from '@/lib/autonomousScan';
import { cn } from '@/lib/utils';

export default function CausalContagionGraph({ proposal, holdings }) {
  const [running, setRunning] = useState(false);
  const [data, setData] = useState(null);

  const run = async () => {
    setRunning(true);
    try {
      const r = await runCausalContagion(proposal, holdings);
      setData(r);
    } catch (e) {
      console.error(e);
    }
    setRunning(false);
  };

  return (
    <div className="rounded-xl border border-border bg-muted/20 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <GitBranch className="w-3.5 h-3.5 text-accent" />
          <span className="text-xs font-semibold">LLM Contagion Assessment</span>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={run}
          disabled={running}
          className="h-7 text-xs gap-1 px-2"
        >
          {running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
          {running ? 'Mapping...' : 'Generate'}
        </Button>
      </div>
      <AnimatePresence>
        {data && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            {data.root_cause && (
              <p className="text-xs text-muted-foreground mb-2">
                <span className="font-medium text-foreground/70">Root cause:</span> {data.root_cause}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-1">
              {(data.nodes || []).map((n, i) => (
                <React.Fragment key={i}>
                  <div className="rounded-lg border border-border bg-card px-2 py-1 text-xs max-w-[150px]">
                    <div className="font-medium truncate">{n.label}</div>
                    {n.mechanism && (
                      <div className="text-muted-foreground text-[10px] leading-tight mt-0.5">
                        {n.mechanism}
                      </div>
                    )}
                  </div>
                  {i < (data.nodes || []).length - 1 && (
                    <ArrowRight className="w-3 h-3 text-muted-foreground/50 flex-shrink-0" />
                  )}
                </React.Fragment>
              ))}
            </div>
            {data.contagion_risk != null && (
              <p
                className={cn(
                  'text-xs mt-2',
                  data.contagion_risk > 60
                    ? 'text-red-400'
                    : data.contagion_risk > 30
                    ? 'text-amber-400'
                    : 'text-emerald-400'
                )}
              >
                Contagion risk: {data.contagion_risk.toFixed(0)}/100
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
