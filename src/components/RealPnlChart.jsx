import React, { useState, useEffect, useMemo } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { TrendingUp, Activity, Loader2, RefreshCw } from 'lucide-react';
import { AreaChart, Area, Tooltip, ResponsiveContainer, XAxis, YAxis, ReferenceLine } from 'recharts';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

function fmtCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(n || 0);
}

function fmtPct(n) {
  const v = n || 0;
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

export default function RealPnlChart() {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const r = await base44.entities.PnlRecord.list('date', 1000);
      setRecords(r || []);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
  }, []);

  const { chartData, stats } = useMemo(() => {
    if (!records.length) return { chartData: [], stats: null };
    const sorted = [...records].sort((a, b) => new Date(a.date) - new Date(b.date));
    const rets = sorted.slice(1).map((r) => (r.daily_return_pct || 0) / 100);
    const mean = rets.reduce((a, b) => a + b, 0) / (rets.length || 1);
    const sd = Math.sqrt(
      rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length || 1)
    );
    const sharpe = sd ? (mean / sd) * Math.sqrt(365) : 0;
    let peak = sorted[0].equity;
    let maxDD = 0;
    for (const r of sorted) {
      if (r.equity > peak) peak = r.equity;
      const dd = (r.equity - peak) / peak;
      if (dd < maxDD) maxDD = dd;
    }
    const last = sorted[sorted.length - 1];
    return {
      chartData: sorted.map((r) => ({
        date: r.date,
        equity: r.equity,
        pnl: r.cumulative_pnl_pct,
      })),
      stats: {
        inception: sorted[0].date,
        end: last.date,
        totalReturn: last.cumulative_pnl_pct,
        finalEquity: last.equity,
        sharpe,
        maxDD: maxDD * 100,
        count: sorted.length,
      },
    };
  }, [records]);

  if (loading && !records.length) {
    return (
      <div className="rounded-2xl border border-border bg-card p-5 mb-6 flex items-center justify-center h-48">
        <Loader2 className="w-5 h-5 animate-spin text-primary" />
      </div>
    );
  }

  if (!stats) return null;

  const positive = stats.totalReturn >= 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-5 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
        <div className="flex items-center gap-2 flex-wrap">
          <Activity className="w-4 h-4 text-emerald-400" />
          <h3 className="font-semibold text-sm">Real P&L Performance</h3>
          <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
            LIVE · Binance BTC/USDT · {stats.count} days
          </span>
        </div>
        <Button size="sm" variant="ghost" onClick={load} disabled={loading} className="h-7 gap-1.5">
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div className="rounded-xl bg-card/60 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Total Return</div>
          <div className={cn('text-lg font-bold font-mono', positive ? 'text-emerald-400' : 'text-red-400')}>
            {fmtPct(stats.totalReturn)}
          </div>
        </div>
        <div className="rounded-xl bg-card/60 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Final Equity</div>
          <div className="text-lg font-bold font-mono">{fmtCurrency(stats.finalEquity)}</div>
        </div>
        <div className="rounded-xl bg-card/60 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Sharpe (ann.)</div>
          <div className={cn('text-lg font-bold font-mono', stats.sharpe >= 0 ? 'text-emerald-400' : 'text-red-400')}>
            {stats.sharpe.toFixed(2)}
          </div>
        </div>
        <div className="rounded-xl bg-card/60 p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Max Drawdown</div>
          <div className="text-lg font-bold font-mono text-red-400">{stats.maxDD.toFixed(2)}%</div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={chartData} margin={{ top: 5, right: 5, left: 5, bottom: 0 }}>
          <defs>
            <linearGradient id="pnlEquity" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="date"
            tick={{ fill: '#94a3b8', fontSize: 10 }}
            tickFormatter={(d) => d.slice(5)}
            minTickGap={40}
            axisLine={{ stroke: '#232b3d' }}
            tickLine={false}
          />
          <YAxis
            tick={{ fill: '#94a3b8', fontSize: 10 }}
            tickFormatter={(v) => fmtCurrency(v)}
            width={60}
            axisLine={false}
            tickLine={false}
          />
          <ReferenceLine y={10000} stroke="#475569" strokeDasharray="3 3" />
          <Tooltip
            contentStyle={{ background: '#131826', border: '1px solid #232b3d', borderRadius: '8px' }}
            labelStyle={{ color: '#94a3b8' }}
            formatter={(v, name) =>
              name === 'equity' ? [fmtCurrency(v), 'Equity'] : [fmtPct(v), 'Cum. P&L']
            }
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#10b981"
            strokeWidth={2}
            fill="url(#pnlEquity)"
          />
        </AreaChart>
      </ResponsiveContainer>

      <p className="text-[10px] text-muted-foreground mt-2">
        Inception {stats.inception} → {stats.end} · $10,000 start · real daily returns from Binance
        BTC/USDT candles · Sharpe & max-drawdown computed from the live return series.
      </p>
    </motion.div>
  );
}