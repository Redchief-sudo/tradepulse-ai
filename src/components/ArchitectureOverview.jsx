import React from 'react';
import { motion } from 'framer-motion';
import {
  Database,
  Brain,
  ShieldCheck,
  Server,
  GitBranch,
  CheckCircle2,
  Circle,
  Loader2,
  Lock,
  X,
  Network,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const PILLARS = [
  {
    icon: Database,
    title: 'Multi-Modal Data Pipelines',
    color: 'text-blue-400',
    items: [
      { label: 'Price & order book / options flow', status: 'active' },
      { label: 'Alternative data (supply chain, foot-traffic)', status: 'active' },
      { label: 'SEC filings & earnings transcripts', status: 'active' },
      { label: 'Global breaking news sentiment', status: 'active' },
    ],
  },
  {
    icon: Brain,
    title: 'Hybrid ML Architectures',
    color: 'text-accent',
    items: [
      { label: 'Transformer — time-series + NLP sentiment (Pass 1)', status: 'active' },
      { label: 'RL / Deep Q-Network — execution & sizing (Pass 2)', status: 'active' },
      { label: 'Graph Neural Network — sector contagion (Pass 3)', status: 'active' },
    ],
  },
  {
    icon: ShieldCheck,
    title: 'Risk Guardrails',
    color: 'text-primary',
    items: [
      { label: 'Position & sector concentration caps', status: 'active' },
      { label: 'Max drawdown thresholds', status: 'active' },
      { label: 'Dynamic volatility-scaled sizing', status: 'active' },
      { label: 'Stop-loss enforcement', status: 'active' },
    ],
  },
  {
    icon: Server,
    title: 'Execution Infrastructure',
    color: 'text-amber-400',
    items: [
      { label: 'Co-location (Equinix NY4)', status: 'planned' },
      { label: 'FPGA model inference', status: 'planned' },
      { label: 'Direct broker API (Builder+ backend)', status: 'locked' },
    ],
  },
];

const ROADMAP = [
  { step: 1, label: 'Data Aggregation', status: 'done' },
  { step: 2, label: 'Feature Engineering', status: 'done' },
  { step: 3, label: 'Model Training', status: 'done' },
  { step: 4, label: 'Risk Layer', status: 'done' },
  { step: 5, label: 'Paper Trading', status: 'current' },
  { step: 6, label: 'Live Deploy', status: 'pending' },
];

function StatusDot({ status }) {
  if (status === 'active' || status === 'done')
    return <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />;
  if (status === 'current')
    return <Loader2 className="w-3.5 h-3.5 text-amber-400 animate-spin flex-shrink-0" />;
  if (status === 'planned')
    return <Circle className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />;
  if (status === 'locked')
    return <Lock className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />;
  return <Circle className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />;
}

export default function ArchitectureOverview({ onClose }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-accent/30 bg-card p-5 md:p-6 mb-6"
    >
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Network className="w-4 h-4 text-accent" />
          <h3 className="font-semibold">Institutional-Grade Architecture</h3>
        </div>
        {onClose && (
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        {PILLARS.map((p) => (
          <div key={p.title} className="rounded-xl border border-border bg-muted/20 p-3">
            <div className="flex items-center gap-2 mb-2">
              <p.icon className={cn('w-4 h-4', p.color)} />
              <span className="text-xs font-semibold">{p.title}</span>
            </div>
            <div className="space-y-1.5">
              {p.items.map((item) => (
                <div key={item.label} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <StatusDot status={item.status} />
                  <span className="leading-snug">{item.label}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* 6-step roadmap */}
      <div className="rounded-xl border border-border bg-muted/20 p-3">
        <div className="flex items-center gap-2 mb-3">
          <GitBranch className="w-4 h-4 text-accent" />
          <span className="text-xs font-semibold">Implementation Roadmap</span>
        </div>
        <div className="flex items-center gap-1 overflow-x-auto scrollbar-thin pb-1">
          {ROADMAP.map((r, i) => (
            <React.Fragment key={r.step}>
              <div
                className={cn(
                  'flex-shrink-0 rounded-lg px-2.5 py-1.5 text-xs border flex items-center gap-1.5 whitespace-nowrap',
                  r.status === 'done' && 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400',
                  r.status === 'current' && 'border-amber-500/30 bg-amber-500/10 text-amber-400',
                  r.status === 'pending' && 'border-border text-muted-foreground'
                )}
              >
                <span className="font-medium">{r.step}.</span>
                <span>{r.label}</span>
                <StatusDot status={r.status} />
              </div>
              {i < ROADMAP.length - 1 && (
                <span className="text-muted-foreground/50 flex-shrink-0">→</span>
              )}
            </React.Fragment>
          ))}
        </div>
      </div>
    </motion.div>
  );
}