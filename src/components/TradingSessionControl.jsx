import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import { Play, Square, Loader2, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useStartTrader } from '@/hooks/useStartTrader';

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

// One-button autonomous trading control.
// Start: activates the AI (trading_active flag) + runs an immediate scan.
// Stop: deactivates the AI + fetches a daily P&L summary.
// The scheduled workflows handle everything in between.
export default function TradingSessionControl({ onComplete, onStart }) {
  const [active, setActive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [summary, setSummary] = useState(null);
  const [summaryError, setSummaryError] = useState(false);
  const trader = useStartTrader();

  const loadStatus = useCallback(async () => {
    try {
      const me = await base44.auth.me();
      setActive(!!me.trading_active);
      setLoadError(false);
    } catch (e) {
      // Show an error state instead of silently defaulting to inactive.
      // (Fixes Rev.9 defect #10: auth/backend failure was presented as
      // "Trading Inactive" rather than "Unable to determine trading state".)
      setLoadError(true);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  const handleStart = async () => {
    setSummary(null);
    try {
      await base44.auth.updateMe({ trading_active: true });
      setActive(true);
      if (onStart) onStart();
      // Run an immediate scan so the user doesn't wait for the next scheduled one
      trader.startTrader({ onComplete });
    } catch (e) {
      console.error(e);
    }
  };

  const handleStop = async () => {
    setStopping(true);
    setSummaryError(false);
    // Stop trading first — this is the critical action and must not be
    // blocked by a summary failure.
    try {
      await base44.auth.updateMe({ trading_active: false });
      setActive(false);
    } catch (e) {
      console.error(e);
    }
    // Fetch summary separately so a summary failure doesn't undo the stop.
    // (Fixes Rev.9 defect #11: stop success + summary failure were combined
    // into one apparent failure with only a console error.)
    try {
      const res = await base44.functions.invoke('sendDailySummary', {});
      const data = res?.data || res;
      if (data?.ok) {
        setSummary(data.summary);
      } else {
        setSummaryError(true);
      }
    } catch (e) {
      setSummaryError(true);
    }
    setStopping(false);
  };

  if (loading) return null;

  return (
    <div className="mb-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className={cn(
          'rounded-2xl border p-5',
          active
            ? 'border-primary/30 bg-gradient-to-br from-primary/10 to-accent/5'
            : 'border-border bg-card'
        )}
      >
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <div className={cn(
              'w-3 h-3 rounded-full',
              active ? 'bg-primary animate-pulse' : 'bg-muted-foreground/40'
            )} />
            <div>
              <h3 className="font-semibold text-sm">
                {loadError
                  ? 'Unable to determine trading state'
                  : active
                    ? 'Autonomous Trading Enabled'
                    : 'Trading Inactive'}
              </h3>
              <p className="text-xs text-muted-foreground">
                {loadError
                  ? 'Check your connection and reload the page'
                  : active
                    ? trader.isRunning
                      ? 'AI is actively scanning and executing trades'
                      : 'AI will scan every 15 minutes during market hours'
                    : 'Press Start and the AI trades for you all day. Press Stop for your P&L.'}
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            {!active ? (
              <Button
                onClick={handleStart}
                disabled={trader.isRunning}
                className="gap-2 bg-gradient-to-r from-primary to-accent"
              >
                {trader.isRunning ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                {trader.isRunning ? 'Starting...' : 'Start Trading'}
              </Button>
            ) : (
              <Button
                onClick={handleStop}
                disabled={stopping || trader.isRunning}
                variant="destructive"
                className="gap-2"
              >
                {stopping ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                {stopping ? 'Calculating...' : 'Stop & See P&L'}
              </Button>
            )}
          </div>
        </div>
      </motion.div>

      {/* Scan progress */}
      {trader.isRunning && trader.stageLabel && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-accent/30 bg-accent/5 p-4 mt-3"
        >
          <div className="flex items-center gap-3">
            <Loader2 className="w-4 h-4 animate-spin text-accent" />
            <p className="text-xs text-muted-foreground">{trader.stageLabel}</p>
          </div>
        </motion.div>
      )}

      {/* Scan error */}
      {!trader.isRunning && trader.error && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-red-500/30 bg-red-500/10 p-4 mt-3"
        >
          <p className="text-xs text-red-500 break-words">{trader.error}</p>
        </motion.div>
      )}

      {/* Daily summary on stop */}
      <AnimatePresence>
        {summary && !active && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="rounded-2xl border border-primary/30 bg-primary/5 p-5 mt-3"
          >
            <div className="flex items-center gap-2 mb-4">
              <CheckCircle2 className="w-5 h-5 text-primary" />
              <h3 className="font-semibold text-sm">Today's Summary</h3>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div>
                <div className="text-xs text-muted-foreground">Portfolio Value</div>
                <div className="text-lg font-bold">{formatCurrency(summary.portfolioValue)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Day's P&L</div>
                <div className={cn('text-lg font-bold', summary.dayPL >= 0 ? 'text-emerald-500' : 'text-red-500')}>
                  {summary.dayPL >= 0 ? '+' : ''}{formatCurrency(summary.dayPL)}
                  <span className="text-xs block opacity-80">
                    ({summary.dayPLPct >= 0 ? '+' : ''}{summary.dayPLPct.toFixed(2)}%)
                  </span>
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">All-Time P&L</div>
                <div className={cn('text-lg font-bold', summary.totalPL >= 0 ? 'text-emerald-500' : 'text-red-500')}>
                  {summary.totalPL >= 0 ? '+' : ''}{formatCurrency(summary.totalPL)}
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Trades Today</div>
                <div className="text-lg font-bold">{summary.tradesToday}</div>
              </div>
            </div>
            <p className="text-xs text-muted-foreground mt-3">
              Summary sent to your email{summary && ' and Telegram'}.
            </p>
          </motion.div>
        )}
        {summaryError && !active && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-4 mt-3"
          >
            <p className="text-xs text-amber-500">
              Trading stopped successfully, but the daily summary failed to generate.
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}