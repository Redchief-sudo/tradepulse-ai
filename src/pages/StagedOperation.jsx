import React, { useState } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import {
  ShieldCheck,
  FlaskConical,
  Eye,
  Rocket,
  Zap,
  CheckCircle2,
  XCircle,
  Loader2,
  Beaker,
  ClipboardCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const STAGES = [
  {
    id: 1,
    name: 'Deterministic Internal Paper',
    icon: ShieldCheck,
    color: 'text-primary',
    description: 'Prove exact accounting, idempotency, stop-loss behavior, model outcome attribution, and restart recovery — all within the app\'s simulated execution engine.',
    criteria: [
      'Exact lot-based fill accounting',
      'Idempotent retries (no duplicate orders)',
      'Correct realized P&L from FIFO lot closure',
      'Stop-loss triggers correctly',
      'Model outcome attribution labels correctly',
      'Restart recovery via order reconciliation',
    ],
    action: 'Run the automated test suite to verify all internal paper scenarios pass.',
  },
  {
    id: 2,
    name: 'Alpaca Broker Paper',
    icon: FlaskConical,
    color: 'text-accent',
    description: 'Run actual Alpaca paper orders and reconcile real broker fills. Target: 100–300 completed round trips with zero unexplained position drift.',
    criteria: [
      '100–300 completed round trips',
      'Zero unexplained position drift',
      'Zero duplicate orders',
      'Zero unreconciled fills',
      'Zero missing realized P&L',
      'Order reconciliation worker recovers timed-out orders',
    ],
    action: 'Connect Alpaca paper credentials in Settings, then run the autonomous scan cycle in paper mode.',
  },
  {
    id: 3,
    name: 'Shadow Live',
    icon: Eye,
    color: 'text-chart-3',
    description: 'Generate live decisions without submitting real orders. Compare expected execution with actual market data and estimate realistic slippage.',
    criteria: [
      'Live market data feeds verified',
      'AI signals generated against live data',
      'Expected vs actual execution comparison',
      'Realistic slippage estimation',
      'No real orders submitted',
    ],
    action: 'Set execution_mode to shadow_live in the autonomous scan cycle and monitor signal quality.',
  },
  {
    id: 4,
    name: 'Limited Live',
    icon: Rocket,
    color: 'text-chart-4',
    description: 'Use one portfolio, minimal capital, a narrow symbol universe, strict daily loss limit, human approval, automatic kill switch, and no autonomous model promotion.',
    criteria: [
      'One portfolio only',
      'Minimal capital ($100–$500)',
      'Narrow symbol universe (3–5 symbols)',
      'Strict daily loss limit (1%)',
      'Human approval for each trade',
      'Automatic kill switch armed',
      'Promotion mode set to Research or Manual Approval',
    ],
    action: 'Connect Alpaca live credentials, set conservative profile, and manually approve each trade.',
  },
  {
    id: 5,
    name: 'Controlled Autonomy',
    icon: Zap,
    color: 'text-primary',
    description: 'Only after stable live evidence should autonomous execution and model promotion be enabled. The system runs on its own with full governance controls.',
    criteria: [
      'Stage 4 stable for 30+ days',
      'Zero critical incidents',
      'Governance gates proven (sample size, OOS, p-value)',
      'Regime-specific champions validated',
      'Audit trail complete and reviewable',
      'Promotion mode set to Automatic',
    ],
    action: 'Enable autonomous scan cycle and set promotion mode to Automatic. Monitor via Operations page.',
  },
];

export default function StagedOperation() {
  const [running, setRunning] = useState(false);
  const [testResults, setTestResults] = useState(null);

  const runTests = async () => {
    setRunning(true);
    setTestResults(null);
    try {
      const result = await base44.functions.invoke('runExecutionTests', {});
      setTestResults(result);
    } catch (e) {
      setTestResults({ error: e.message });
    }
    setRunning(false);
  };

  return (
    <div className="p-4 md:p-8 pb-24 md:pb-8 max-w-4xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight flex items-center gap-2">
          <ClipboardCheck className="w-7 h-7 text-primary" />
          Staged Operation
        </h1>
        <p className="text-muted-foreground text-sm mt-1">
          Progressive validation pipeline — prove the system at each stage before advancing
        </p>
      </div>

      {/* Test suite runner */}
      <div className="rounded-2xl border border-border bg-card p-5 mb-6">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Beaker className="w-4 h-4 text-accent" />
            <h2 className="font-semibold">Automated Test Suite</h2>
          </div>
          <Button onClick={runTests} disabled={running} size="sm" className="gap-2">
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Beaker className="w-4 h-4" />}
            {running ? 'Running...' : 'Run Tests'}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground mb-3">
          Verifies: rejected order zero mutation, full buy/sell accounting, partial sells, retry idempotency,
          cross-user isolation, and governance gates. Creates and cleans up its own test data.
        </p>
        {testResults && (
          <div className="space-y-2">
            {testResults.error ? (
              <div className="text-sm text-red-500 flex items-center gap-2">
                <XCircle className="w-4 h-4" /> {testResults.error}
              </div>
            ) : (
              <>
                <div className="flex items-center gap-4 text-sm">
                  <span className="text-emerald-500 flex items-center gap-1">
                    <CheckCircle2 className="w-4 h-4" /> {testResults.passed} passed
                  </span>
                  {testResults.failed > 0 && (
                    <span className="text-red-500 flex items-center gap-1">
                      <XCircle className="w-4 h-4" /> {testResults.failed} failed
                    </span>
                  )}
                  <span className="text-muted-foreground">/ {testResults.total} total</span>
                </div>
                <div className="space-y-1 max-h-48 overflow-y-auto scrollbar-thin">
                  {testResults.results?.map((r, i) => (
                    <div key={i} className="flex items-start gap-2 text-xs">
                      {r.passed ? (
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0 mt-0.5" />
                      ) : (
                        <XCircle className="w-3.5 h-3.5 text-red-500 flex-shrink-0 mt-0.5" />
                      )}
                      <span className={r.passed ? 'text-muted-foreground' : 'text-red-400'}>
                        {r.test}
                        {!r.passed && r.detail && ` — ${r.detail}`}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Stage cards */}
      <div className="space-y-4">
        {STAGES.map((stage, idx) => (
          <motion.div
            key={stage.id}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: idx * 0.05 }}
            className="rounded-2xl border border-border bg-card p-5"
          >
            <div className="flex items-start gap-3 mb-3">
              <div className={cn('rounded-xl bg-secondary p-2.5 flex-shrink-0', stage.color)}>
                <stage.icon className="w-5 h-5" />
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono text-muted-foreground">Stage {stage.id}</span>
                </div>
                <h3 className="font-semibold text-sm">{stage.name}</h3>
              </div>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed mb-3">{stage.description}</p>
            <div className="space-y-1.5 mb-3">
              {stage.criteria.map((c, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <CheckCircle2 className="w-3.5 h-3.5 text-primary/60 flex-shrink-0 mt-0.5" />
                  <span className="text-muted-foreground">{c}</span>
                </div>
              ))}
            </div>
            <div className="rounded-lg bg-secondary/50 p-3 text-xs text-muted-foreground italic">
              {stage.action}
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}