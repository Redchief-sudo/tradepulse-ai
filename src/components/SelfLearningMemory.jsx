import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Brain, Loader2, Play, TrendingUp, AlertCircle, Stethoscope } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { runSelfLearning } from '@/lib/autonomousScan';
import { cn } from '@/lib/utils';

const FACTORS = [
  { key: 'technical', label: 'Technical', color: 'bg-blue-500' },
  { key: 'fundamental', label: 'Fundamental', color: 'bg-emerald-500' },
  { key: 'sentiment', label: 'Sentiment', color: 'bg-violet-500' },
  { key: 'momentum', label: 'Momentum', color: 'bg-amber-500' },
  { key: 'risk', label: 'Risk', color: 'bg-red-500' },
];

const DEFAULT_WEIGHTS = { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };

export default function SelfLearningMemory({ decisions, onWeights }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);

  const run = async () => {
    setRunning(true);
    try {
      const r = await runSelfLearning(decisions);
      setResult(r);
      if (r.weights && onWeights) onWeights(r.weights);
    } catch (e) {
      console.error(e);
    }
    setRunning(false);
  };

  const weights = result?.weights || DEFAULT_WEIGHTS;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-5 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <div className="flex items-center gap-2 flex-wrap">
          <Brain className="w-4 h-4 text-accent" />
          <h3 className="font-semibold text-sm">Self-Learning Model Memory</h3>
          {result?.accuracy != null && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
              <TrendingUp className="w-3 h-3" /> {result.accuracy.toFixed(0)}% accuracy
            </span>
          )}
        </div>
        <Button
          size="sm"
          variant="secondary"
          onClick={run}
          disabled={running || !decisions.length}
          className="gap-1.5"
        >
          {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
          {running ? 'Learning...' : 'Run Post-Mortem'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
        Diagnoses past trade losses (bad timing, flawed sentiment, or macro events) and dynamically
        adjusts the 5 quant factor weights fed into the ML scoring engine.
      </p>

      <div className="space-y-2 mb-3">
        {FACTORS.map((f) => {
          const val = weights[f.key] || 0;
          const defaultVal = DEFAULT_WEIGHTS[f.key];
          const delta = val - defaultVal;
          return (
            <div key={f.key}>
              <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-muted-foreground">{f.label}</span>
                <span className="font-medium">
                  {val.toFixed(0)}%
                  {result && delta !== 0 && (
                    <span className={cn('ml-1.5', delta > 0 ? 'text-emerald-500' : 'text-red-500')}>
                      ({delta > 0 ? '+' : ''}
                      {delta.toFixed(0)})
                    </span>
                  )}
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                <div
                  className={cn('h-full rounded-full transition-all', f.color)}
                  style={{ width: `${Math.min((val / 40) * 100, 100)}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>

      <AnimatePresence>
        {result?.post_mortems?.length > 0 && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-2">
            <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Stethoscope className="w-3.5 h-3.5" /> Loss Post-Mortems
            </div>
            {result.post_mortems.map((pm, i) => (
              <div key={i} className="rounded-lg border border-red-500/20 bg-red-500/5 p-2 text-xs">
                <div className="flex items-center gap-1.5 mb-0.5">
                  <AlertCircle className="w-3 h-3 text-red-400" />
                  <span className="font-medium text-red-400">{pm.symbol}</span>
                  <span className="text-muted-foreground">· {pm.cause}</span>
                </div>
                <p className="text-muted-foreground leading-snug pl-4">{pm.lesson}</p>
              </div>
            ))}
            {result.summary && (
              <p className="text-xs text-muted-foreground italic">{result.summary}</p>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {!decisions.length && (
        <p className="text-xs text-muted-foreground italic">
          No past decisions yet — run a scan and execute trades to enable self-learning.
        </p>
      )}
    </motion.div>
  );
}