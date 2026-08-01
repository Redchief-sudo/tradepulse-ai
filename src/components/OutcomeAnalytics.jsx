import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Microscope, Loader2, RefreshCw, TrendingUp, Activity, Clock } from 'lucide-react';
import { cn } from '@/lib/utils';

function fmtPct(n, signed = true) {
  if (n == null) return '—';
  return `${signed && n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

function StatCard({ label, value, sub, tone }) {
  const toneClass = tone === 'pos' ? 'text-emerald-400' : tone === 'neg' ? 'text-red-400' : 'text-foreground';
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('text-lg font-mono font-semibold mt-0.5', toneClass)}>{value}</div>
      {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  );
}

export default function OutcomeAnalytics() {
  const [decisions, setDecisions] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const all = await base44.entities.AITradeDecision.list('-created_date', 200);
      const labeled = (all || []).filter(
        (d) => d.outcome_status === 'realized' || d.realized_return != null || d.return_1d != null
      );
      setDecisions(labeled);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const retKey = (d) => d.realized_return ?? d.return_1d ?? null;

  // Alpha vs benchmark
  const withBenchmark = decisions.filter((d) => d.benchmark_return != null && d.realized_return != null);
  const avgAlpha =
    withBenchmark.length
      ? withBenchmark.reduce((s, d) => s + (d.realized_return - d.benchmark_return), 0) / withBenchmark.length
      : null;
  const avgRealized =
    decisions.length ? decisions.reduce((s, d) => s + (retKey(d) ?? 0), 0) / decisions.length : null;
  const avgBenchmark =
    withBenchmark.length ? withBenchmark.reduce((s, d) => s + d.benchmark_return, 0) / withBenchmark.length : null;
  const beatBenchmark = withBenchmark.filter((d) => d.realized_return > d.benchmark_return).length;
  const beatPct = withBenchmark.length ? (beatBenchmark / withBenchmark.length) * 100 : null;

  // Forward return evolution
  const horizons = [
    { key: 'return_5m', label: '5m' },
    { key: 'return_1h', label: '1h' },
    { key: 'return_1d', label: '1d' },
    { key: 'return_5d', label: '5d' },
  ];
  const forwardData = horizons
    .map((h) => {
      const vals = decisions.filter((d) => d[h.key] != null).map((d) => d[h.key]);
      return { horizon: h.label, avg: vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0, count: vals.length };
    })
    .filter((h) => h.count > 0);

  // Factor predictive power
  const factors = [
    { key: 'technical_score', label: 'Technical' },
    { key: 'fundamental_score', label: 'Fundamental' },
    { key: 'sentiment_score', label: 'Sentiment' },
    { key: 'momentum_score', label: 'Momentum' },
    { key: 'risk_score', label: 'Risk' },
  ];
  const factorPower = factors.map((f) => {
    const withScore = decisions.filter((d) => d[f.key] != null && retKey(d) != null);
    if (withScore.length < 2) return { factor: f.label, correlation: null, highWinRate: null, lowWinRate: null, n: withScore.length };
    const sorted = [...withScore].sort((a, b) => a[f.key] - b[f.key]);
    const mid = Math.floor(sorted.length / 2);
    const low = sorted.slice(0, mid);
    const high = sorted.slice(mid);
    const highWin = high.filter((d) => retKey(d) > 0).length;
    const lowWin = low.filter((d) => retKey(d) > 0).length;
    const n = withScore.length;
    const meanX = withScore.reduce((s, d) => s + d[f.key], 0) / n;
    const meanY = withScore.reduce((s, d) => s + retKey(d), 0) / n;
    let cov = 0, varX = 0, varY = 0;
    withScore.forEach((d) => {
      const dx = d[f.key] - meanX, dy = retKey(d) - meanY;
      cov += dx * dy; varX += dx * dx; varY += dy * dy;
    });
    const corr = varX > 0 && varY > 0 ? cov / Math.sqrt(varX * varY) : 0;
    return {
      factor: f.label,
      correlation: Math.round(corr * 100) / 100,
      highWinRate: high.length ? Math.round((highWin / high.length) * 100) : null,
      lowWinRate: low.length ? Math.round((lowWin / low.length) * 100) : null,
      n: withScore.length,
    };
  });

  // Excursion analysis
  const withExcursion = decisions.filter((d) => d.max_adverse_excursion != null && d.max_favorable_excursion != null);
  const avgMAE = withExcursion.length ? withExcursion.reduce((s, d) => s + d.max_adverse_excursion, 0) / withExcursion.length : null;
  const avgMFE = withExcursion.length ? withExcursion.reduce((s, d) => s + d.max_favorable_excursion, 0) / withExcursion.length : null;
  const rewardRisk = avgMAE != null && avgMFE != null && avgMAE !== 0 ? avgMFE / Math.abs(avgMAE) : null;

  // Holding period
  const withHolding = decisions.filter((d) => d.holding_period_minutes != null);
  const avgHolding = withHolding.length ? withHolding.reduce((s, d) => s + d.holding_period_minutes, 0) / withHolding.length : null;

  const hasData = decisions.length > 0;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden mb-8">
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Microscope className="w-4 h-4 text-chart-3" />
          <h3 className="font-semibold">Outcome Analytics</h3>
          <span className="text-xs text-muted-foreground">· labeled forward returns & factor validation</span>
        </div>
        <button onClick={load} disabled={loading} className="text-muted-foreground hover:text-foreground transition-colors">
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
        </button>
      </div>

      {loading ? (
        <div className="p-8 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading labeled outcomes...
        </div>
      ) : !hasData ? (
        <div className="p-8 text-center">
          <Activity className="w-10 h-10 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm text-muted-foreground max-w-md mx-auto">
            No labeled outcomes yet. The outcome-labeling workflow runs on a schedule, populating forward
            returns (5m, 1h, 1d, 5d), benchmark alpha, and excursion data on each AI decision once enough
            time has elapsed.
          </p>
        </div>
      ) : (
        <div className="p-5 space-y-5">
          {/* Alpha summary */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <StatCard
              label="Alpha vs SPY"
              value={fmtPct(avgAlpha)}
              sub={withBenchmark.length ? `${withBenchmark.length} labeled` : 'no benchmark data'}
              tone={avgAlpha != null ? (avgAlpha >= 0 ? 'pos' : 'neg') : undefined}
            />
            <StatCard
              label="Avg Realized Return"
              value={fmtPct(avgRealized)}
              sub={`vs ${fmtPct(avgBenchmark)} benchmark`}
              tone={avgRealized != null ? (avgRealized >= 0 ? 'pos' : 'neg') : undefined}
            />
            <StatCard
              label="Beat Benchmark"
              value={beatPct != null ? `${beatPct.toFixed(0)}%` : '—'}
              sub={withBenchmark.length ? `${beatBenchmark}/${withBenchmark.length} trades` : ''}
              tone={beatPct != null ? (beatPct >= 50 ? 'pos' : 'neg') : undefined}
            />
            <StatCard
              label="Reward / Risk"
              value={rewardRisk != null ? rewardRisk.toFixed(2) : '—'}
              sub={avgMFE != null ? `${fmtPct(avgMFE, false)} up / ${fmtPct(avgMAE, false)} down` : ''}
              tone={rewardRisk != null ? (rewardRisk >= 1 ? 'pos' : 'neg') : undefined}
            />
          </div>

          {/* Forward return evolution */}
          {forwardData.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <TrendingUp className="w-3.5 h-3.5" /> Forward Return Evolution
              </h4>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={forwardData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                  <XAxis dataKey="horizon" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                  <YAxis tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={(v) => `${v.toFixed(1)}%`} />
                  <Tooltip
                    contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12 }}
                    formatter={(v, _n, p) => [fmtPct(v), `Avg return (${p.payload.count} pts)`]}
                  />
                  <Bar dataKey="avg" radius={[4, 4, 0, 0]}>
                    {forwardData.map((d, i) => (
                      <Cell key={i} fill={d.avg >= 0 ? 'hsl(var(--primary))' : 'hsl(var(--destructive))'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Factor predictive power */}
          <div>
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
              Factor Predictive Power
            </h4>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-muted-foreground text-xs border-b border-border">
                    <th className="text-left font-medium p-2">Factor</th>
                    <th className="text-right font-medium p-2">Correlation</th>
                    <th className="text-right font-medium p-2">High-Score Win%</th>
                    <th className="text-right font-medium p-2">Low-Score Win%</th>
                    <th className="text-right font-medium p-2">Edge</th>
                    <th className="text-right font-medium p-2">N</th>
                  </tr>
                </thead>
                <tbody>
                  {factorPower.map((f) => {
                    const edge = f.highWinRate != null && f.lowWinRate != null ? f.highWinRate - f.lowWinRate : null;
                    return (
                      <tr key={f.factor} className="border-b border-border/30">
                        <td className="p-2 font-medium">{f.factor}</td>
                        <td className={cn('text-right p-2 font-mono text-xs', f.correlation != null && f.correlation > 0 ? 'text-emerald-400' : f.correlation != null && f.correlation < 0 ? 'text-red-400' : 'text-muted-foreground')}>
                          {f.correlation != null ? f.correlation.toFixed(2) : '—'}
                        </td>
                        <td className="text-right p-2 font-mono text-xs">{f.highWinRate != null ? `${f.highWinRate}%` : '—'}</td>
                        <td className="text-right p-2 font-mono text-xs">{f.lowWinRate != null ? `${f.lowWinRate}%` : '—'}</td>
                        <td className={cn('text-right p-2 font-mono text-xs', edge != null && edge > 0 ? 'text-emerald-400' : edge != null && edge < 0 ? 'text-red-400' : 'text-muted-foreground')}>
                          {edge != null ? `${edge > 0 ? '+' : ''}${edge}%` : '—'}
                        </td>
                        <td className="text-right p-2 font-mono text-xs text-muted-foreground">{f.n}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Holding period */}
          {avgHolding != null && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Clock className="w-3.5 h-3.5" />
              Avg holding period: <span className="font-mono text-foreground">{avgHolding < 60 ? `${avgHolding.toFixed(0)}m` : avgHolding < 1440 ? `${(avgHolding / 60).toFixed(1)}h` : `${(avgHolding / 1440).toFixed(1)}d`}</span>
              <span className="text-muted-foreground/60">·</span>
              {decisions.length} labeled decisions
            </div>
          )}
        </div>
      )}
    </motion.div>
  );
}