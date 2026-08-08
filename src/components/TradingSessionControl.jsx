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
  const [sessionState, setSessionState] = useState('disabled');
  const [killSwitchResetRequired, setKillSwitchResetRequired] = useState(false);
  const [killSwitchReason, setKillSwitchReason] = useState(null);
  const [integrityManualRequired, setIntegrityManualRequired] = useState(false);
  const [integrityRecoveredAt, setIntegrityRecoveredAt] = useState(null);
  const [integrityReason, setIntegrityReason] = useState(null);
  const trader = useStartTrader();

  const loadStatus = useCallback(async () => {
    try {
      const me = await base44.auth.me();
      setActive(!!me.trading_active);
      setSessionState(me.trading_session_state || 'disabled');
      setKillSwitchResetRequired(!!me.kill_switch_reset_required);
      setKillSwitchReason(me.kill_switch_reason || null);
      setIntegrityManualRequired(!!me.financial_integrity_manual_reenable_required);
      setIntegrityRecoveredAt(me.financial_integrity_recovered_at || null);
      setIntegrityReason(me.financial_integrity_reason || null);
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
      await base44.auth.updateMe({
        trading_active: true,
        trading_session_state: 'active',
        kill_switch_reset_required: false,
        kill_switch_reason: null,
        kill_switch_at: null,
      });
      setActive(true);
      setSessionState('active');
      setKillSwitchResetRequired(false);
      setKillSwitchReason(null);
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
      await base44.auth.updateMe({ trading_active: false, trading_session_state: 'manually_stopped' });
      setActive(false);
      setSessionState('manually_stopped');
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
        setSummary({ ...data.summary, emailSent: data.emailSent, telegramSent: data.telegramSent });
      } else {
        setSummaryError(true);
      }
    } catch (e) {
      setSummaryError(true);
    }
    setStopping(false);
  };

  const handleResetKillSwitch = async () => {
    try {
      await base44.auth.updateMe({
        kill_switch_reset_required: false,
        kill_switch_reason: null,
        kill_switch_at: null,
        trading_session_state: 'disabled',
        trading_active: false,
      });
      setKillSwitchResetRequired(false);
      setKillSwitchReason(null);
      setSessionState('disabled');
      setActive(false);
    } catch (e) {
      console.error(e);
    }
  };

  const handleAcknowledgeIntegrityRecovery = async () => {
    try {
      await base44.auth.updateMe({
        trading_active: false,
        trading_session_state: 'disabled',
        financial_integrity_manual_reenable_required: false,
        financial_integrity_reason: null,
        financial_integrity_recovered_at: null,
      });
      setIntegrityManualRequired(false);
      setIntegrityRecoveredAt(null);
      setIntegrityReason(null);
      setSessionState('disabled');
    } catch (e) {
      console.error(e);
    }
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
                  : killSwitchResetRequired
                    ? 'Kill Switch Active — Trading Stopped'
                    : integrityManualRequired
                      ? integrityRecoveredAt
                        ? 'Settlement Recovered — Manual Re-enable Required'
                        : 'Financial Integrity Block — Trading Stopped'
                    : sessionState === 'risk_stopped'
                      ? 'Risk Stopped — Manual Reset Required'
                      : sessionState === 'broker_unavailable'
                        ? 'Broker Unavailable'
                        : sessionState === 'system_degraded'
                          ? 'System Degraded'
                          : sessionState === 'market_closed'
                            ? 'Market Closed'
                            : active
                              ? 'Autonomous Trading Enabled'
                              : 'Trading Inactive'}
              </h3>
              <p className="text-xs text-muted-foreground">
                {loadError
                  ? 'Check your connection and reload the page'
                  : killSwitchReason
                    ? `Reason: ${killSwitchReason}`
                    : integrityManualRequired
                      ? integrityRecoveredAt
                        ? 'All settlement events passed integrity verification. Acknowledge recovery before restarting.'
                        : `Settlement recovery is incomplete. ${integrityReason || 'Review settlement and reconciliation events.'}`
                    : active
                      ? trader.isRunning
                        ? 'AI is actively scanning and executing trades'
                        : 'AI will scan every 15 minutes during market hours'
                      : 'Press Start and the AI trades for you all day. Press Stop for your P&L.'}
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            {killSwitchResetRequired && (
              <Button
                onClick={handleResetKillSwitch}
                variant="outline"
                className="gap-2 border-amber-500/50 text-amber-500 hover:bg-amber-500/10"
              >
                Reset Kill Switch
              </Button>
            )}
            {integrityManualRequired && integrityRecoveredAt && (
              <Button
                onClick={handleAcknowledgeIntegrityRecovery}
                variant="outline"
                className="gap-2 border-amber-500/50 text-amber-500 hover:bg-amber-500/10"
              >
                Acknowledge Recovery
              </Button>
            )}
            {!active ? (
              <Button
                onClick={handleStart}
                disabled={trader.isRunning || killSwitchResetRequired || integrityManualRequired}
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

      {/* Kill switch alert */}
      {killSwitchResetRequired && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-red-500/30 bg-red-500/10 p-4 mt-3"
        >
          <p className="text-xs text-red-500 font-semibold">
            ⚠ Kill Switch Active
          </p>
          <p className="text-xs text-red-500/80 mt-1">
            {killSwitchReason || 'Daily loss limit reached'}. All new buys are blocked and pending orders were canceled.
            Press "Reset Kill Switch" to acknowledge and resume.
          </p>
        </motion.div>
      )}

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
              {summary.brokerEquity != null && (
                <div>
                  <div className="text-xs text-muted-foreground">Alpaca Equity</div>
                  <div className="text-lg font-bold text-primary">{formatCurrency(summary.brokerEquity)}</div>
                </div>
              )}
              <div>
                <div className="text-xs text-muted-foreground">{summary.brokerEquity != null ? 'App Position Value' : 'Portfolio Value'}</div>
                <div className="text-lg font-bold">{formatCurrency(summary.portfolioValue)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">{summary.brokerDayPL != null ? 'Alpaca Day P&L' : "Day's P&L"}</div>
                <div className={cn('text-lg font-bold', (summary.brokerDayPL != null ? summary.brokerDayPL : summary.dayPL) >= 0 ? 'text-emerald-500' : 'text-red-500')}>
                  {(summary.brokerDayPL != null ? summary.brokerDayPL : summary.dayPL) >= 0 ? '+' : ''}
                  {formatCurrency(summary.brokerDayPL != null ? summary.brokerDayPL : summary.dayPL)}
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
            {summary.reconciliationDiff != null && (
              <div className="mt-3 p-3 rounded-lg bg-muted/30 border border-border">
                <div className="text-xs text-muted-foreground">Reconciliation Difference (broker - app)</div>
                <div className="text-sm font-semibold">{formatCurrency(summary.reconciliationDiff)}</div>
                <p className="text-xs text-muted-foreground mt-1">
                  Includes cash, fees, and unreconciled fills not tracked in app positions.
                </p>
              </div>
            )}
            <p className="text-xs text-muted-foreground mt-3">
              {summary.emailSent !== false ? '✓ Summary sent to email' : '✗ Email delivery failed'}
              {summary.telegramSent ? ' and Telegram' : ''}
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
