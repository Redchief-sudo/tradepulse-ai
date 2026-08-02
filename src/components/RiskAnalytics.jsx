import React, { useState, useCallback, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Shield, Loader2, RefreshCw, AlertTriangle, Activity } from 'lucide-react';
import { cn } from '@/lib/utils';

function fmtPct(n, signed = false) {
  if (n == null) return '—';
  return `${signed && n >= 0 ? '+' : ''}${(n * 100).toFixed(2)}%`;
}

function fmtNum(n, digits = 2) {
  if (n == null) return '—';
  return n.toFixed(digits);
}

function fmtCurrency(n) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n || 0);
}

// Correlation cell color: -1 (red) → 0 (neutral) → 1 (green)
function corrColor(c) {
  if (c >= 0) return `rgba(16, 185, 129, ${0.12 + c * 0.38})`;
  return `rgba(239, 68, 68, ${0.12 + Math.abs(c) * 0.38})`;
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

export default function RiskAnalytics({ holdings }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const portfolioValue = (holdings || []).reduce(
    (s, h) => s + h.shares * (h.current_price || h.avg_price),
    0
  );

  const load = useCallback(async () => {
    if (!holdings || holdings.length === 0) return;
    setLoading(true);
    setError('');
    try {
      const result = await base44.functions.invoke('getRiskAnalytics', {});
      if (result?.data?.ok) {
        setData(result.data);
      } else {
        setError(result?.data?.error || 'Failed to compute risk metrics');
      }
    } catch (e) {
      setError(e.message || 'Failed to load risk analytics');
    }
    setLoading(false);
  }, [holdings]);

  if (!holdings || holdings.length === 0) return null;

  const p = data?.portfolio;
  const varDollar = p ? portfolioValue * Math.abs(p.histVar95) : null;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden mb-8">
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4 text-chart-3" />
          <h3 className="font-semibold">Portfolio Risk Analytics</h3>
          <span className="text-xs text-muted-foreground">· volatility · VaR · beta · correlation</span>
        </div>
        <button onClick={load} disabled={loading} className="text-muted-foreground hover:text-foreground transition-colors">
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
        </button>
      </div>

      {loading ? (
        <div className="p-8 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" /> Fetching historical data & computing risk metrics...
        </div>
      ) : error ? (
        <div className="p-6">
          <div className="flex items-start gap-2 text-sm text-muted-foreground mb-3">
            <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
          <button onClick={load} className="text-sm text-primary hover:underline">Retry</button>
        </div>
      ) : !data ? (
        <div className="p-8 text-center">
          <Activity className="w-10 h-10 mx-auto text-muted-foreground mb-3" />
          <p className="text-sm text-muted-foreground max-w-md mx-auto mb-4">
            Compute portfolio volatility, Value at Risk, beta vs SPY, Sharpe ratio, max drawdown,
            and the pairwise correlation matrix from 1 year of historical price data.
          </p>
          <button onClick={load} className="text-sm text-primary hover:underline font-medium">
            Compute Risk Metrics
          </button>
        </div>
      ) : p ? (
        <div className="p-5 space-y-5">
          {/* Risk summary cards */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
            <StatCard
              label="Annual Volatility"
              value={fmtPct(p.annualVol)}
              sub="1-year realized"
              tone={p.annualVol > 0.25 ? 'neg' : undefined}
            />
            <StatCard
              label="1-Day VaR (95%)"
              value={fmtPct(p.histVar95)}
              sub={varDollar != null ? `${fmtCurrency(varDollar)} at risk` : ''}
              tone="neg"
            />
            <StatCard
              label="Beta vs SPY"
              value={fmtNum(p.beta)}
              sub={p.beta > 1.2 ? 'high sensitivity' : p.beta < 0.8 ? 'low sensitivity' : 'market-like'}
              tone={p.beta > 1.2 ? 'neg' : undefined}
            />
            <StatCard
              label="Sharpe Ratio"
              value={fmtNum(p.sharpe)}
              sub={`ann. return ${fmtPct(p.annualReturn, true)}`}
              tone={p.sharpe >= 1 ? 'pos' : p.sharpe < 0 ? 'neg' : undefined}
            />
            <StatCard
              label="Max Drawdown"
              value={fmtPct(p.maxDrawdown)}
              sub="trailing 1-year"
              tone="neg"
            />
          </div>

          {/* Correlation matrix */}
          {data.correlation && data.correlation.symbols.length > 1 && (
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Correlation Matrix
              </h4>
              <div className="overflow-x-auto">
                <table className="border-collapse">
                  <thead>
                    <tr>
                      <th className="p-1"></th>
                      {data.correlation.symbols.map((s) => (
                        <th key={s} className="text-xs font-medium text-muted-foreground p-1.5 text-center min-w-[48px]">
                          {s}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.correlation.symbols.map((sym, i) => (
                      <tr key={sym}>
                        <td className="text-xs font-medium text-muted-foreground p-1.5 text-right pr-2">{sym}</td>
                        {data.correlation.matrix[i].map((c, j) => (
                          <td
                            key={j}
                            className="text-center text-xs font-mono p-1.5 border border-border/30"
                            style={{ background: corrColor(c) }}
                            title={`${sym} vs ${data.correlation.symbols[j]}: ${c.toFixed(2)}`}
                          >
                            {i === j ? '1.00' : c.toFixed(2)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Individual holding risk */}
          {data.individual && data.individual.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                Holding-Level Risk
              </h4>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-muted-foreground text-xs border-b border-border">
                      <th className="text-left font-medium p-2">Symbol</th>
                      <th className="text-right font-medium p-2">Weight</th>
                      <th className="text-right font-medium p-2">Volatility</th>
                      <th className="text-right font-medium p-2">Beta</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.individual.map((h) => (
                      <tr key={h.symbol} className="border-b border-border/30">
                        <td className="p-2 font-medium">{h.symbol}</td>
                        <td className="text-right p-2 font-mono text-xs">{fmtPct(h.weight)}</td>
                        <td className={cn('text-right p-2 font-mono text-xs', h.annualVol > 0.4 ? 'text-red-400' : h.annualVol > 0.25 ? 'text-amber-400' : 'text-emerald-400')}>
                          {fmtPct(h.annualVol)}
                        </td>
                        <td className={cn('text-right p-2 font-mono text-xs', h.beta > 1.2 ? 'text-red-400' : h.beta < 0.8 ? 'text-emerald-400' : 'text-muted-foreground')}>
                          {fmtNum(h.beta)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <p className="text-xs text-muted-foreground/70 italic">
            Computed from {data.observations} daily observations · benchmark: {data.benchmark} · parametric VaR: {fmtPct(p.paramVar95)}
          </p>
        </div>
      ) : null}
    </motion.div>
  );
}