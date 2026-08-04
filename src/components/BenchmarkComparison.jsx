import React, { useState, useCallback, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { BarChart3, Loader2, RefreshCw, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

const TIMEFRAMES = [
  { key: '1M', days: 21 },
  { key: '3M', days: 63 },
  { key: '6M', days: 126 },
  { key: '1Y', days: 252 },
];

function fmtPct(n, signed = false) {
  if (n == null) return '—';
  return `${signed && n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;
}

function fmtNum(n, digits = 2) {
  if (n == null) return '—';
  return n.toFixed(digits);
}

function StatPill({ label, value, tone }) {
  const toneClass = tone === 'pos' ? 'text-emerald-400' : tone === 'neg' ? 'text-red-400' : 'text-foreground';
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3 flex-1 min-w-[100px]">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn('text-lg font-mono font-semibold mt-0.5', toneClass)}>{value}</div>
    </div>
  );
}

export default function BenchmarkComparison({ holdings, autoTriggerKey }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [timeframe, setTimeframe] = useState('3M');

  const load = useCallback(async () => {
    if (!holdings || holdings.length === 0) return;
    setLoading(true);
    setError('');
    try {
      const result = await base44.functions.invoke('getBenchmarkComparison', {});
      if (result?.data?.ok) {
        setData(result.data);
      } else {
        setError(result?.data?.error || 'Failed to load benchmark comparison');
      }
    } catch (e) {
      setError(e.message || 'Failed to load benchmark comparison');
    }
    setLoading(false);
  }, [holdings]);

  useEffect(() => {
    if (autoTriggerKey > 0) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoTriggerKey]);

  if (!holdings || holdings.length === 0) return null;

  const tf = TIMEFRAMES.find((t) => t.key === timeframe);
  const fullData = data?.chartData || [];
  const sliced = tf ? fullData.slice(-tf.days) : fullData;
  const metrics = data?.timeframes?.[timeframe];

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-chart-2" />
          <h3 className="font-semibold">Benchmark Comparison</h3>
          <span className="text-xs text-muted-foreground">· portfolio vs SPY</span>
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
          <Loader2 className="w-4 h-4 animate-spin" /> Fetching historical data & computing benchmark comparison...
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
          <BarChart3 className="w-10 h-10 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm text-muted-foreground max-w-md mx-auto mb-4">
            Compare your portfolio's performance against the S&amp;P 500 (SPY) benchmark. See cumulative
            returns, alpha, tracking error, and information ratio across multiple timeframes.
          </p>
          <button onClick={load} className="text-sm text-primary hover:underline font-medium">
            Compare vs Benchmark
          </button>
        </div>
      ) : (
        <div className="p-5 space-y-4">
          {/* Timeframe selector */}
          <div className="flex gap-1.5">
            {TIMEFRAMES.map((t) => (
              <button
                key={t.key}
                onClick={() => setTimeframe(t.key)}
                className={cn(
                  'px-3 py-1 rounded-md text-xs font-medium transition-colors',
                  timeframe === t.key
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted text-muted-foreground hover:bg-muted/80'
                )}
              >
                {t.key}
              </button>
            ))}
          </div>

          {/* Stats */}
          {metrics && (
            <div className="flex flex-wrap gap-2">
              <StatPill
                label="Portfolio"
                value={fmtPct(metrics.portReturn, true)}
                tone={metrics.portReturn >= 0 ? 'pos' : 'neg'}
              />
              <StatPill
                label="SPY"
                value={fmtPct(metrics.benchReturn, true)}
                tone={metrics.benchReturn >= 0 ? 'pos' : 'neg'}
              />
              <StatPill
                label="Alpha"
                value={fmtPct(metrics.alpha, true)}
                tone={metrics.alpha >= 0 ? 'pos' : 'neg'}
              />
              <StatPill label="Tracking Error" value={fmtPct(metrics.trackingError)} />
              <StatPill
                label="Info Ratio"
                value={fmtNum(metrics.informationRatio)}
                tone={metrics.informationRatio >= 0 ? 'pos' : 'neg'}
              />
            </div>
          )}

          {/* Overlay chart */}
          {sliced.length > 1 && (
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={sliced} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.3} />
                <XAxis
                  dataKey="date"
                  tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                  tickFormatter={(d) => d.slice(5)}
                  minTickGap={40}
                />
                <YAxis
                  tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                  domain={['auto', 'auto']}
                  width={50}
                />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--card))',
                    border: '1px solid hsl(var(--border))',
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                  labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
                />
                <Line
                  type="monotone"
                  dataKey="portfolio"
                  stroke="hsl(var(--chart-1))"
                  strokeWidth={2}
                  dot={false}
                  name="Portfolio"
                />
                <Line
                  type="monotone"
                  dataKey="benchmark"
                  stroke="hsl(var(--muted-foreground))"
                  strokeWidth={1.5}
                  dot={false}
                  strokeDasharray="4 4"
                  name="SPY"
                />
              </LineChart>
            </ResponsiveContainer>
          )}

          <p className="text-xs text-muted-foreground/70 italic">
            Rebased to 100 at start of period · {data.observations} daily observations · benchmark: {data.benchmark}
          </p>
        </div>
      )}
    </motion.div>
  );
}