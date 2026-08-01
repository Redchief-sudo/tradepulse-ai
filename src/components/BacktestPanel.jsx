import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Play, Loader2, FlaskConical } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

const STRATEGIES = [
  { id: 'sma_cross', label: 'SMA Crossover (20/50)' },
  { id: 'rsi_reversion', label: 'RSI Mean-Reversion (14, 30/70)' },
  { id: 'breakout', label: 'Donchian Breakout (20)' },
];

function fmtPct(n) { return n == null ? '—' : `${n.toFixed(2)}%`; }
function fmtMoney(n) { return n == null ? '—' : `$${Number(n).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`; }

function MetricCard({ label, value, tone }) {
  const toneClass = tone === 'pos' ? 'text-emerald-400' : tone === 'neg' ? 'text-red-400' : 'text-foreground';
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-lg font-mono font-semibold mt-0.5 ${toneClass}`}>{value}</div>
    </div>
  );
}

export default function BacktestPanel() {
  const [symbol, setSymbol] = useState('AAPL');
  const [strategy, setStrategy] = useState('sma_cross');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [capital, setCapital] = useState(10000);
  const [walkForward, setWalkForward] = useState(false);
  const [trainSize, setTrainSize] = useState(252);
  const [testSize, setTestSize] = useState(63);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await base44.functions.invoke('runBacktest', {
        symbol,
        strategy,
        from: from || undefined,
        to: to || undefined,
        initialCapital: capital,
        walk_forward: walkForward,
        trainSize: walkForward ? trainSize : undefined,
        testSize: walkForward ? testSize : undefined,
      });
      if (res?.data?.error) throw new Error(res.data.error);
      setResult(res.data);
    } catch (e) {
      setError(e.message || 'Backtest failed');
    }
    setRunning(false);
  };

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden mb-6">
      <div className="p-5 border-b border-border">
        <h3 className="font-semibold flex items-center gap-2"><FlaskConical className="w-4 h-4 text-chart-3" /> Strategy Backtest & Walk-Forward</h3>
        <p className="text-xs text-muted-foreground mt-1">Deterministic, bar-by-bar simulation with transaction costs. No LLM estimation.</p>
      </div>

      {/* Form */}
      <div className="p-5 grid grid-cols-2 md:grid-cols-4 gap-3 border-b border-border">
        <div>
          <Label className="text-xs">Symbol</Label>
          <Input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} className="mt-1" placeholder="AAPL" />
        </div>
        <div>
          <Label className="text-xs">Strategy</Label>
          <Select value={strategy} onValueChange={setStrategy}>
            <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
            <SelectContent>
              {STRATEGIES.map((s) => <SelectItem key={s.id} value={s.id}>{s.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">From (optional)</Label>
          <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="mt-1" />
        </div>
        <div>
          <Label className="text-xs">To (optional)</Label>
          <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="mt-1" />
        </div>
        <div>
          <Label className="text-xs">Initial Capital</Label>
          <Input type="number" value={capital} onChange={(e) => setCapital(Number(e.target.value))} className="mt-1" />
        </div>
        <div className="flex items-end gap-2">
          <Button variant={walkForward ? 'outline' : 'default'} onClick={() => setWalkForward(false)} size="sm">Backtest</Button>
          <Button variant={walkForward ? 'default' : 'outline'} onClick={() => setWalkForward(true)} size="sm">Walk-Forward</Button>
        </div>
        {walkForward && (
          <>
            <div>
              <Label className="text-xs">Train Size (bars)</Label>
              <Input type="number" value={trainSize} onChange={(e) => setTrainSize(Number(e.target.value))} className="mt-1" />
            </div>
            <div>
              <Label className="text-xs">Test Size (bars)</Label>
              <Input type="number" value={testSize} onChange={(e) => setTestSize(Number(e.target.value))} className="mt-1" />
            </div>
          </>
        )}
        <div className="col-span-2 md:col-span-4 flex justify-end">
          <Button onClick={run} disabled={running} className="gap-2 bg-gradient-to-r from-primary to-accent">
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            {running ? 'Running...' : 'Run Backtest'}
          </Button>
        </div>
      </div>

      {/* Error */}
      {error && <div className="p-4 text-sm text-red-400 border-b border-border">{error}</div>}

      {/* Backtest results */}
      {result && result.mode === 'backtest' && (
        <div className="p-5 space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-2">
            <MetricCard label="Total Return" value={fmtPct(result.metrics.total_return_pct)} tone={result.metrics.total_return_pct >= 0 ? 'pos' : 'neg'} />
            <MetricCard label="CAGR" value={fmtPct(result.metrics.cagr_pct)} tone={result.metrics.cagr_pct >= 0 ? 'pos' : 'neg'} />
            <MetricCard label="Sharpe" value={result.metrics.sharpe?.toFixed(2)} />
            <MetricCard label="Sortino" value={result.metrics.sortino?.toFixed(2)} />
            <MetricCard label="Max Drawdown" value={fmtPct(result.metrics.max_drawdown_pct)} tone="neg" />
            <MetricCard label="Win Rate" value={fmtPct(result.metrics.win_rate_pct)} />
            <MetricCard label="Profit Factor" value={result.metrics.profit_factor?.toFixed(2)} />
            <MetricCard label="Trades" value={result.metrics.num_trades} />
            <MetricCard label="Exposure" value={fmtPct(result.metrics.exposure_pct)} />
            <MetricCard label="Total Costs" value={fmtMoney(result.metrics.total_costs)} tone="neg" />
          </div>

          <div>
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">Equity Curve</h4>
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={result.equity_curve}>
                <defs>
                  <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} minTickGap={40} />
                <YAxis tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`} />
                <Tooltip contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12 }} />
                <Area type="monotone" dataKey="equity" stroke="hsl(var(--primary))" strokeWidth={2} fill="url(#eqGrad)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {result.trades?.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">Trades ({result.trades.length})</h4>
              <div className="overflow-auto max-h-60">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-card">
                    <tr className="text-muted-foreground text-xs border-b border-border">
                      <th className="text-left font-medium p-2">Date</th>
                      <th className="text-left font-medium p-2">Side</th>
                      <th className="text-right font-medium p-2">Qty</th>
                      <th className="text-right font-medium p-2">Price</th>
                      <th className="text-right font-medium p-2">Cost</th>
                      <th className="text-right font-medium p-2">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t, i) => (
                      <tr key={i} className="border-b border-border/30">
                        <td className="p-2 text-xs">{t.date}</td>
                        <td className="p-2"><span className={t.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{t.side}</span></td>
                        <td className="text-right p-2 font-mono text-xs">{t.qty}</td>
                        <td className="text-right p-2 font-mono text-xs">${t.price?.toFixed(2)}</td>
                        <td className="text-right p-2 font-mono text-xs text-muted-foreground">${t.cost?.toFixed(2)}</td>
                        <td className="text-right p-2 font-mono text-xs">{t.pnl != null ? <span className={t.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>${t.pnl.toFixed(2)}</span> : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Walk-forward results */}
      {result && result.mode === 'walk_forward' && (
        <div className="p-5 space-y-4">
          {result.error ? (
            <p className="text-sm text-red-400">{result.error}</p>
          ) : (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                <MetricCard label="Folds Run" value={result.summary?.folds_run} />
                <MetricCard label="Avg IS Return" value={fmtPct(result.summary?.avg_in_sample_return_pct)} />
                <MetricCard label="Avg OOS Return" value={fmtPct(result.summary?.avg_out_of_sample_return_pct)} tone={result.summary?.avg_out_of_sample_return_pct >= 0 ? 'pos' : 'neg'} />
                <MetricCard label="Overfitting Ratio" value={result.summary?.overfitting_ratio?.toFixed(2)} tone={result.summary?.overfitting_ratio >= 0.5 ? 'pos' : 'neg'} />
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-muted-foreground text-xs border-b border-border">
                      <th className="text-left font-medium p-2">Test Period</th>
                      <th className="text-right font-medium p-2">IS Return</th>
                      <th className="text-right font-medium p-2">OOS Return</th>
                      <th className="text-right font-medium p-2">IS Sharpe</th>
                      <th className="text-right font-medium p-2">OOS Sharpe</th>
                      <th className="text-right font-medium p-2">Degradation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.folds?.map((f, i) => (
                      <tr key={i} className="border-b border-border/30">
                        <td className="p-2 text-xs">{f.test_start} → {f.test_end}</td>
                        <td className="text-right p-2 font-mono text-xs">{fmtPct(f.in_sample.total_return_pct)}</td>
                        <td className="text-right p-2 font-mono text-xs">{fmtPct(f.out_of_sample.total_return_pct)}</td>
                        <td className="text-right p-2 font-mono text-xs">{f.in_sample.sharpe?.toFixed(2)}</td>
                        <td className="text-right p-2 font-mono text-xs">{f.out_of_sample.sharpe?.toFixed(2)}</td>
                        <td className="text-right p-2 font-mono text-xs">{f.degradation_sharpe?.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </motion.div>
  );
}