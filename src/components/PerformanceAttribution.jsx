import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Activity, Award, AlertTriangle, Clock, Gauge, Layers, TrendingDown, TrendingUp } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { Loader2 } from 'lucide-react';

function fmtCurrency(n) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(n || 0);
}
function fmtMs(ms) {
  if (!ms || ms < 1) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
function fmtPct(n) {
  return `${(n || 0).toFixed(1)}%`;
}

const STRATEGY_LABELS = {
  autonomous: 'Autonomous AI',
  stoploss: 'Stop-Loss',
  dashboard_exit: 'Dashboard Exit',
  ai_assistant: 'AI Assistant',
  autonomous_ui: 'Autonomous (UI)',
  stoploss_ui: 'Stop-Loss (UI)',
  manual: 'Manual',
  reconciliation: 'Reconciliation',
};

function HealthPill({ label, value, tone }) {
  const tones = {
    good: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
    warn: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
    bad: 'text-red-400 bg-red-500/10 border-red-500/20',
    neutral: 'text-muted-foreground bg-muted/40 border-border',
  };
  return (
    <div className={`rounded-lg border px-3 py-2 ${tones[tone] || tones.neutral}`}>
      <div className="text-[10px] uppercase tracking-wider opacity-80">{label}</div>
      <div className="text-lg font-semibold font-mono">{value}</div>
    </div>
  );
}

export default function PerformanceAttribution() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await base44.functions.invoke('getPerformanceAttribution', {});
      setData(res.data);
    } catch (e) {
      setError(e.message || 'Failed to load attribution');
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  if (loading) {
    return (
      <div className="rounded-2xl border border-border bg-card p-8 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin text-primary" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-2xl border border-border bg-card p-5">
        <div className="flex items-center gap-2 text-red-400 text-sm">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      </div>
    );
  }
  if (!data) return null;

  const { strategies, health, totals, byAssetClass } = data;
  const hasData = strategies.length > 0 || health.totalIntents > 0;

  return (
    <div className="space-y-4">
      {/* Execution Health */}
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card p-5">
        <h3 className="font-semibold mb-4 flex items-center gap-2">
          <Activity className="w-4 h-4 text-primary" />
          Execution Health
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
          <HealthPill label="Intents" value={health.totalIntents} tone="neutral" />
          <HealthPill label="Fill Rate" value={fmtPct(health.fillRate)} tone={health.fillRate >= 80 ? 'good' : health.fillRate >= 50 ? 'warn' : 'bad'} />
          <HealthPill label="Rejection Rate" value={fmtPct(health.rejectionRate)} tone={health.rejectionRate <= 10 ? 'good' : health.rejectionRate <= 30 ? 'warn' : 'bad'} />
          <HealthPill label="Avg Latency" value={fmtMs(health.avgLatencyMs)} tone={health.avgLatencyMs < 2000 ? 'good' : 'warn'} />
          <HealthPill label="P95 Latency" value={fmtMs(health.p95LatencyMs)} tone={health.p95LatencyMs < 5000 ? 'good' : 'warn'} />
          <HealthPill label="Total Cost" value={fmtCurrency(health.totalCost)} tone="neutral" />
        </div>

        {/* Status breakdown */}
        <div className="mt-4 flex flex-wrap gap-2">
          {Object.entries(health.byStatus).map(([status, count]) => (
            <span key={status} className="text-xs px-2 py-1 rounded-md bg-muted/40 border border-border text-muted-foreground">
              {status}: <span className="font-mono text-foreground">{count}</span>
            </span>
          ))}
        </div>

        {/* Rejection reasons */}
        {health.rejectionReasons.length > 0 && (
          <div className="mt-4">
            <div className="text-xs text-muted-foreground mb-2 flex items-center gap-1">
              <AlertTriangle className="w-3 h-3" /> Top Rejection Reasons
            </div>
            <div className="space-y-1">
              {health.rejectionReasons.map((r) => (
                <div key={r.reason} className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground truncate max-w-[70%]">{r.reason}</span>
                  <span className="font-mono text-red-400">{r.count}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Venue breakdown */}
        {health.byVenue.length > 0 && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground border-b border-border">
                  <th className="text-left font-medium py-2">Venue</th>
                  <th className="text-right font-medium py-2">Fills</th>
                  <th className="text-right font-medium py-2">Notional</th>
                  <th className="text-right font-medium py-2">Commissions</th>
                  <th className="text-right font-medium py-2">Slippage</th>
                </tr>
              </thead>
              <tbody>
                {health.byVenue.map((v) => (
                  <tr key={v.venue} className="border-b border-border/30">
                    <td className="py-2 capitalize">{v.venue}</td>
                    <td className="text-right py-2 font-mono">{v.fills}</td>
                    <td className="text-right py-2 font-mono">{fmtCurrency(v.notional)}</td>
                    <td className="text-right py-2 font-mono">{fmtCurrency(v.commissions)}</td>
                    <td className="text-right py-2 font-mono">{fmtCurrency(v.slippage)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </motion.div>

      {/* Asset Class Attribution */}
      {byAssetClass && byAssetClass.length > 0 && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden">
          <div className="p-5 border-b border-border">
            <h3 className="font-semibold flex items-center gap-2">
              <Layers className="w-4 h-4 text-chart-3" />
              Asset Class Attribution
            </h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground text-xs border-b border-border">
                  <th className="text-left font-medium p-3">Asset Class</th>
                  <th className="text-right font-medium p-3">Trades</th>
                  <th className="text-right font-medium p-3 hidden sm:table-cell">Win Rate</th>
                  <th className="text-right font-medium p-3 hidden md:table-cell">Realized</th>
                  <th className="text-right font-medium p-3 hidden lg:table-cell">Open P&L</th>
                  <th className="text-right font-medium p-3 hidden md:table-cell">Notional</th>
                  <th className="text-right font-medium p-3">Total P&L</th>
                </tr>
              </thead>
              <tbody>
                {byAssetClass.map((a) => (
                  <tr key={a.assetClass} className="border-b border-border/30 hover:bg-muted/20 transition-colors">
                    <td className="p-3 font-medium capitalize">{a.assetClass}</td>
                    <td className="text-right p-3 font-mono text-xs">{a.trades}</td>
                    <td className="text-right p-3 hidden sm:table-cell font-mono text-xs">
                      {a.sells > 0 ? fmtPct(a.winRate) : '—'}
                    </td>
                    <td className="text-right p-3 hidden md:table-cell font-mono text-xs">
                      <span className={a.realizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(a.realizedPnl)}</span>
                    </td>
                    <td className="text-right p-3 hidden lg:table-cell font-mono text-xs">
                      <span className={a.openPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(a.openPnl)}</span>
                    </td>
                    <td className="text-right p-3 hidden md:table-cell font-mono text-xs text-muted-foreground">{fmtCurrency(a.notional)}</td>
                    <td className="text-right p-3 font-mono font-semibold">
                      <span className={a.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(a.totalPnl)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </motion.div>
      )}

      {/* Strategy Attribution */}
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden">
        <div className="p-5 border-b border-border flex items-center justify-between">
          <h3 className="font-semibold flex items-center gap-2">
            <Award className="w-4 h-4 text-accent" />
            Strategy Attribution
          </h3>
          <div className="text-xs text-muted-foreground">
            Net P&L: <span className={`font-mono ${totals.netPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{fmtCurrency(totals.netPnl)}</span>
            <span className="mx-2">·</span>
            Open: <span className="font-mono">{fmtCurrency(totals.openPnl)}</span>
          </div>
        </div>

        {strategies.length === 0 ? (
          <div className="p-8 text-center text-sm text-muted-foreground">No executed trades yet — attribution appears after the first fills settle.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground text-xs border-b border-border">
                  <th className="text-left font-medium p-3">Strategy</th>
                  <th className="text-right font-medium p-3">Trades</th>
                  <th className="text-right font-medium p-3 hidden sm:table-cell">Win Rate</th>
                  <th className="text-right font-medium p-3 hidden md:table-cell">Realized</th>
                  <th className="text-right font-medium p-3 hidden lg:table-cell">Open P&L</th>
                  <th className="text-right font-medium p-3 hidden md:table-cell">Costs</th>
                  <th className="text-right font-medium p-3">Net P&L</th>
                  <th className="text-right font-medium p-3">Total</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s) => (
                  <tr key={s.strategy} className="border-b border-border/30 hover:bg-muted/20 transition-colors">
                    <td className="p-3 font-medium">{STRATEGY_LABELS[s.strategy] || s.strategy}</td>
                    <td className="text-right p-3 font-mono text-xs">{s.trades}</td>
                    <td className="text-right p-3 hidden sm:table-cell font-mono text-xs">
                      <span className={s.winRate >= 50 ? 'text-emerald-400' : s.winRate > 0 ? 'text-amber-400' : 'text-muted-foreground'}>
                        {s.sells > 0 ? fmtPct(s.winRate) : '—'}
                      </span>
                    </td>
                    <td className="text-right p-3 hidden md:table-cell font-mono text-xs">
                      <span className={s.realizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(s.realizedPnl)}</span>
                    </td>
                    <td className="text-right p-3 hidden lg:table-cell font-mono text-xs">
                      <span className={s.openPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(s.openPnl)}</span>
                    </td>
                    <td className="text-right p-3 hidden md:table-cell font-mono text-xs text-muted-foreground">{fmtCurrency(s.commissions)}</td>
                    <td className="text-right p-3 font-mono text-xs">
                      <span className={s.netPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(s.netPnl)}</span>
                    </td>
                    <td className="text-right p-3 font-mono font-semibold">
                      <span className={s.totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtCurrency(s.totalPnl)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </motion.div>
    </div>
  );
}