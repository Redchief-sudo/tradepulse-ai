import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Award, TrendingUp, TrendingDown, Target, Loader2, RefreshCw } from 'lucide-react';
import { formatCurrency } from '@/lib/portfolio';
import { cn } from '@/lib/utils';

export default function TradePerformance({ decisions }) {
  const [performance, setPerformance] = useState(null);
  const [loading, setLoading] = useState(false);

  const compute = useCallback(async () => {
    const buyDecisions = (decisions || []).filter(
      (d) => d.action === 'buy' && d.status === 'executed'
    );
    if (!buyDecisions.length) {
      setPerformance(null);
      return;
    }
    setLoading(true);
    try {
      // Real quotes via the same backend price endpoint StopLossScanner.jsx
      // uses — not an LLM guess. Fixes a prior defect: this used to ask an
      // LLM to "get the current stock prices," a hallucination-prone,
      // non-authoritative source feeding a P&L figure shown to the user.
      const symbols = [...new Set(buyDecisions.map((d) => d.symbol))];
      const BATCH_SIZE = 40; // matches getMultiAssetQuotes/entry.ts's hard cap
      const prices = {};
      for (let i = 0; i < symbols.length; i += BATCH_SIZE) {
        const batch = symbols.slice(i, i + BATCH_SIZE);
        const items = batch.map((symbol) => ({ symbol, asset_class: 'stocks' }));
        const result = await base44.functions.invoke('getMultiAssetQuotes', { items });
        const quotes = (result.data?.quotes || result.quotes || []).filter((q) => !q.error);
        quotes.forEach((q) => {
          prices[q.symbol.toUpperCase()] = q.price;
        });
      }

      const scored = buyDecisions.map((d) => {
        const current = prices[d.symbol.toUpperCase()] || d.price;
        const pnl = (current - d.price) * d.shares;
        const pnlPct = d.price > 0 ? ((current - d.price) / d.price) * 100 : 0;
        return { ...d, current_price: current, pnl, pnlPct };
      });

      const wins = scored.filter((s) => s.pnl > 0);
      const totalPnl = scored.reduce((sum, s) => sum + s.pnl, 0);
      const avgReturn =
        scored.length > 0
          ? scored.reduce((sum, s) => sum + s.pnlPct, 0) / scored.length
          : 0;

      setPerformance({
        total: scored.length,
        wins: wins.length,
        winRate: scored.length > 0 ? (wins.length / scored.length) * 100 : 0,
        avgReturn,
        totalPnl,
        scored: scored.sort((a, b) => b.pnl - a.pnl),
      });
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  }, [decisions]);

  useEffect(() => {
    compute();
  }, [compute]);

  if (!performance && !loading) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Award className="w-4 h-4 text-accent" />
          <h3 className="font-semibold">AI Track Record</h3>
        </div>
        <button
          onClick={compute}
          disabled={loading}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          {loading ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <RefreshCw className="w-4 h-4" />
          )}
        </button>
      </div>

      {loading ? (
        <div className="p-8 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" />
          Scoring past decisions...
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-5">
            <div>
              <div className="text-xs text-muted-foreground mb-1">Total Trades</div>
              <div className="text-xl font-bold">{performance.total}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground mb-1">Win Rate</div>
              <div
                className={cn(
                  'text-xl font-bold',
                  performance.winRate >= 50 ? 'text-emerald-500' : 'text-red-500'
                )}
              >
                {performance.winRate.toFixed(0)}%
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground mb-1">Avg Return</div>
              <div
                className={cn(
                  'text-xl font-bold',
                  performance.avgReturn >= 0 ? 'text-emerald-500' : 'text-red-500'
                )}
              >
                {performance.avgReturn >= 0 ? '+' : ''}
                {performance.avgReturn.toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground mb-1">Total P&L</div>
              <div
                className={cn(
                  'text-xl font-bold',
                  performance.totalPnl >= 0 ? 'text-emerald-500' : 'text-red-500'
                )}
              >
                {performance.totalPnl >= 0 ? '+' : ''}
                {formatCurrency(performance.totalPnl)}
              </div>
            </div>
          </div>

          <div className="border-t border-border divide-y divide-border/50 max-h-64 overflow-y-auto scrollbar-thin">
            {performance.scored.map((s, i) => (
              <div key={i} className="flex items-center justify-between p-3 px-5 text-sm">
                <div className="flex items-center gap-2">
                  {s.pnl >= 0 ? (
                    <TrendingUp className="w-3.5 h-3.5 text-emerald-500" />
                  ) : (
                    <TrendingDown className="w-3.5 h-3.5 text-red-500" />
                  )}
                  <span className="font-medium">{s.symbol}</span>
                  <span className="text-xs text-muted-foreground">
                    {s.shares} @ {formatCurrency(s.price)}
                  </span>
                  {s.target_price > 0 && (
                    <span className="text-xs text-muted-foreground hidden sm:inline">
                      <Target className="w-3 h-3 inline mr-1" />
                      {formatCurrency(s.target_price)}
                    </span>
                  )}
                </div>
                <div className="text-right">
                  <span className={cn('font-medium', s.pnl >= 0 ? 'text-emerald-500' : 'text-red-500')}>
                    {s.pnl >= 0 ? '+' : ''}
                    {formatCurrency(s.pnl)}
                  </span>
                  <span className="text-xs text-muted-foreground ml-2">
                    ({s.pnlPct >= 0 ? '+' : ''}
                    {s.pnlPct.toFixed(1)}%)
                  </span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </motion.div>
  );
}