import React, { useState, useEffect, useCallback } from 'react';
import { motion } from 'framer-motion';
import { Activity, Loader2, RefreshCw, CheckCircle2, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { fetchCryptoUniverse, CRYPTO_SYMBOLS } from '@/lib/marketData';
import { computeRealFactors } from '@/lib/quantScoring';
import { cn } from '@/lib/utils';

function fmt(n, d = 2) {
  if (n == null || Number.isNaN(n)) return '—';
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: d }).format(n);
}

export default function RealDataPipeline() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [rows, setRows] = useState([]);
  const [updated, setUpdated] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const universe = await fetchCryptoUniverse(CRYPTO_SYMBOLS.map((s) => s.symbol));
      const computed = universe.map((u) => {
        const meta = CRYPTO_SYMBOLS.find((s) => s.symbol === u.symbol) || {};
        const f = computeRealFactors(u.candles);
        return { symbol: u.symbol, label: meta.label, ticker: meta.ticker, quote: u.quote, factors: f };
      });
      setRows(computed);
      setUpdated(new Date());
    } catch (e) {
      setError(e.message || 'Failed to fetch live data');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-5 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
        <div className="flex items-center gap-2 flex-wrap">
          <Activity className="w-4 h-4 text-emerald-400" />
          <h3 className="font-semibold text-sm">Real Market Data Pipeline</h3>
          <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
            <CheckCircle2 className="w-3 h-3" /> LIVE · Binance · Computed Indicators
          </span>
        </div>
        <Button size="sm" variant="ghost" onClick={load} disabled={loading} className="h-7 gap-1.5">
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Refresh
        </Button>
      </div>
      <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
        Live OHLCV from Binance public API. RSI, MACD, Bollinger, volatility, and factor scores are
        computed with their published formulas — precise, not LLM-estimated.
      </p>

      {error && (
        <div className="rounded-lg p-3 text-xs bg-red-500/10 text-red-400 flex items-center gap-2 mb-3">
          <AlertCircle className="w-4 h-4" /> {error}
        </div>
      )}

      <div className="overflow-x-auto scrollbar-thin">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted-foreground border-b border-border">
              <th className="text-left font-medium p-2">Asset</th>
              <th className="text-right font-medium p-2">Price</th>
              <th className="text-right font-medium p-2">24h</th>
              <th className="text-right font-medium p-2">RSI(14)</th>
              <th className="text-right font-medium p-2">MACD</th>
              <th className="text-right font-medium p-2">Volatility</th>
              <th className="text-right font-medium p-2">Momentum</th>
              <th className="text-right font-medium p-2">Technical</th>
              <th className="text-right font-medium p-2">Risk</th>
              <th className="text-right font-medium p-2">Composite</th>
            </tr>
          </thead>
          <tbody>
            {loading && rows.length === 0 ? (
              <tr>
                <td colSpan={10} className="p-4 text-center text-muted-foreground">
                  <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                  Fetching live candles…
                </td>
              </tr>
            ) : (
              rows.map((r) => {
                const f = r.factors || {};
                const up = (r.quote?.changePercent || 0) >= 0;
                return (
                  <tr key={r.symbol} className="border-b border-border/40">
                    <td className="p-2">
                      <div className="font-medium">{r.ticker}</div>
                      <div className="text-muted-foreground text-[10px]">{r.label}</div>
                    </td>
                    <td className="p-2 text-right font-mono">${fmt(r.quote?.price)}</td>
                    <td className={cn('p-2 text-right font-mono', up ? 'text-emerald-400' : 'text-red-400')}>
                      {up ? '+' : ''}{fmt(r.quote?.changePercent)}%
                    </td>
                    <td className={cn('p-2 text-right font-mono', (f.rsi || 50) < 30 ? 'text-emerald-400' : (f.rsi || 50) > 70 ? 'text-red-400' : '')}>
                      {fmt(f.rsi, 1)}
                    </td>
                    <td className={cn('p-2 text-right font-mono', (f.macd?.histogram || 0) > 0 ? 'text-emerald-400' : 'text-red-400')}>
                      {f.macd ? (f.macd.histogram > 0 ? '▲' : '▼') : '—'}
                    </td>
                    <td className="p-2 text-right font-mono">{fmt(f.volatility, 1)}%</td>
                    <td className={cn('p-2 text-right font-mono', (f.momentum || 0) >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                      {f.momentum != null ? `${f.momentum >= 0 ? '+' : ''}${fmt(f.momentum)}%` : '—'}
                    </td>
                    <td className="p-2 text-right font-mono">{fmt(f.technical_score, 0)}</td>
                    <td className="p-2 text-right font-mono">{fmt(f.risk_score, 0)}</td>
                    <td className={cn('p-2 text-right font-mono font-semibold', (f.composite || 0) >= 60 ? 'text-emerald-400' : (f.composite || 0) < 45 ? 'text-red-400' : '')}>
                      {fmt(f.composite, 0)}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {updated && (
        <p className="text-[10px] text-muted-foreground mt-2">
          Last updated {updated.toLocaleTimeString()} · {rows.length} assets · real Binance OHLCV
        </p>
      )}
    </motion.div>
  );
}