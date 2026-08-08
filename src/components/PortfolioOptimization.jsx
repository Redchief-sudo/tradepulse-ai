import React, { useState, useCallback, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ScatterChart,
  Scatter,
} from 'recharts';
import { Target, Loader2, RefreshCw, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

function fmtPct(n, signed = false) {
  if (n == null) return '—';
  return `${signed && n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;
}

function fmtNum(n, digits = 2) {
  if (n == null) return '—';
  return n.toFixed(digits);
}

function ComparisonStat({ label, currentValue, optimalValue, format = 'pct' }) {
  const fmt = format === 'num' ? fmtNum : fmtPct;
  const better = optimalValue != null && currentValue != null && optimalValue > currentValue;
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3 flex-1 min-w-[120px]">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <div>
          <div className="text-[10px] text-muted-foreground/70 uppercase">Current</div>
          <div className="text-sm font-mono font-semibold">{fmt(currentValue)}</div>
        </div>
        <div className="text-muted-foreground/50">→</div>
        <div>
          <div className="text-[10px] text-muted-foreground/70 uppercase">Optimal</div>
          <div className={cn('text-sm font-mono font-semibold', better && 'text-emerald-400')}>
            {fmt(optimalValue)}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function PortfolioOptimization({ holdings, autoTriggerKey }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!holdings || holdings.length < 2) return;
    setLoading(true);
    setError('');
    try {
      const result = await base44.functions.invoke('getPortfolioOptimization', {});
      if (result?.data?.ok) {
        setData(result.data);
      } else {
        setError(result?.data?.error || 'Failed to compute optimization');
      }
    } catch (e) {
      setError(e.message || 'Failed to load optimization');
    }
    setLoading(false);
  }, [holdings]);

  useEffect(() => {
    if (autoTriggerKey > 0) load();

  }, [autoTriggerKey]);

  if (!holdings || holdings.length < 2) return null;

  const frontierData = (data?.efficientFrontier || []).map((p) => ({
    x: Math.round(p.volatility * 1000) / 10,
    y: Math.round(p.return * 1000) / 10,
  }));
  const currentPoint =
    data?.current
      ? [{ x: Math.round(data.current.volatility * 1000) / 10, y: Math.round(data.current.expectedReturn * 1000) / 10 }]
      : [];
  const optimalPoint =
    data?.optimal
      ? [{ x: Math.round(data.optimal.volatility * 1000) / 10, y: Math.round(data.optimal.expectedReturn * 1000) / 10 }]
      : [];

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Target className="w-4 h-4 text-chart-4" />
          <h3 className="font-semibold">Portfolio Optimization</h3>
          <span className="text-xs text-muted-foreground">· Markowitz efficient frontier</span>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
        </button>
      </div>

      {loading ? (
        <div className="p-8 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" /> Computing efficient frontier & optimal weights...
        </div>
      ) : error ? (
        <div className="p-6">
          <div className="flex items-start gap-2 text-sm text-muted-foreground mb-3">
            <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
          <button onClick={load} className="text-sm text-primary hover:underline">
            Retry
          </button>
        </div>
      ) : !data ? (
        <div className="p-8 text-center">
          <Target className="w-10 h-10 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm text-muted-foreground max-w-md mx-auto mb-4">
            Compute the Markowitz efficient frontier and find the maximum Sharpe ratio portfolio.
            Compare your current allocation against the optimal risk-adjusted weights.
          </p>
          <button onClick={load} className="text-sm text-primary hover:underline font-medium">
            Optimize Portfolio
          </button>
        </div>
      ) : (
        <div className="p-5 space-y-4">
          {/* Current vs Optimal stats */}
          <div className="flex flex-wrap gap-2">
            <ComparisonStat
              label="Expected Return"
              currentValue={data.current.expectedReturn}
              optimalValue={data.optimal?.expectedReturn}
            />
            <ComparisonStat
              label="Volatility"
              currentValue={data.current.volatility}
              optimalValue={data.optimal?.volatility}
            />
            <ComparisonStat
              label="Sharpe Ratio"
              currentValue={data.current.sharpe}
              optimalValue={data.optimal?.sharpe}
              format="num"
            />
          </div>

          <div className="grid md:grid-cols-2 gap-4">
            {/* Weights comparison bar chart */}
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Current vs Optimal Weights
              </h4>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={data.weightComparison} margin={{ top: 5, right: 5, left: -15, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
                  <XAxis dataKey="symbol" tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }} />
                  <YAxis
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                    tickFormatter={(v) => `${v}%`}
                    width={45}
                  />
                  <Tooltip
                    contentStyle={{
                      background: 'hsl(var(--card))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(v) => `${v}%`}
                  />
                  <Bar dataKey="current" fill="hsl(var(--chart-3))" name="Current" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="optimal" fill="hsl(var(--chart-1))" name="Optimal" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Efficient frontier scatter plot */}
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Efficient Frontier
              </h4>
              <ResponsiveContainer width="100%" height={220}>
                <ScatterChart margin={{ top: 5, right: 10, left: -15, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
                  <XAxis
                    type="number"
                    dataKey="x"
                    name="Volatility"
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                    tickFormatter={(v) => `${v}%`}
                    label={{ value: 'Vol', position: 'insideBottom', offset: -2, fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
                  />
                  <YAxis
                    type="number"
                    dataKey="y"
                    name="Return"
                    tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                    tickFormatter={(v) => `${v}%`}
                    width={45}
                  />
                  <Tooltip
                    contentStyle={{
                      background: 'hsl(var(--card))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: '8px',
                      fontSize: '12px',
                    }}
                    formatter={(v) => `${v}%`}
                  />
                  <Scatter data={frontierData} fill="hsl(var(--muted-foreground))" opacity={0.4} name="Frontier" />
                  <Scatter data={currentPoint} fill="hsl(var(--chart-3))" name="Current" />
                  <Scatter data={optimalPoint} fill="hsl(var(--chart-1))" name="Optimal" />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          </div>

          <p className="text-xs text-muted-foreground/70 italic">
            {data.observations} daily observations · long-only constraint applied · risk-free rate = 0 ·
            optimal = max Sharpe (tangency) portfolio
          </p>
        </div>
      )}
    </motion.div>
  );
}
