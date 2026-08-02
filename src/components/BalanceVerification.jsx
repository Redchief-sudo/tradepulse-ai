import React, { useState, useCallback, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldCheck, Loader2, RefreshCw, AlertTriangle, CheckCircle2, XCircle, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

export default function BalanceVerification({ holdings }) {
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [events, setEvents] = useState([]);

  const loadEvents = useCallback(async () => {
    try {
      const recent = await base44.entities.ReconciliationEvent.list('-run_timestamp', 20);
      setEvents(recent || []);
    } catch (e) {
      // entity may not exist yet or no events
    }
  }, []);

  useEffect(() => {
    loadEvents();
  }, [loadEvents]);

  const verify = async () => {
    setVerifying(true);
    setError(null);
    setResult(null);
    try {
      const res = await base44.functions.invoke('syncBrokerPositions', {});
      setResult(res.data || res);
      await loadEvents();
    } catch (e) {
      setError(e.message || 'Verification failed');
    }
    setVerifying(false);
  };

  const appTotalValue = holdings.reduce(
    (sum, h) => sum + h.shares * (h.current_price || h.avg_price),
    0
  );

  const summary = result?.summary || {};
  const hasDrift = (summary.qty_drift || 0) > 0 || (summary.adjustments || 0) > 0;
  const allMatch = result && !hasDrift && (summary.matched || 0) > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-5 mb-8"
    >
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-primary" />
          <h3 className="font-semibold">Portfolio Balance Verification</h3>
        </div>
        <Button
          onClick={verify}
          disabled={verifying}
          variant={verifying ? 'secondary' : 'default'}
          className="gap-2"
        >
          {verifying ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          {verifying ? 'Verifying...' : 'Verify Balance'}
        </Button>
      </div>

      <p className="text-sm text-muted-foreground mb-4">
        Compares your app holdings against your actual Alpaca broker positions. Any quantity or price
        drift is flagged for review, and externally closed positions are synced automatically.
      </p>

      {/* App-side summary always visible */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <div className="rounded-lg bg-muted/40 p-3">
          <div className="text-xs text-muted-foreground mb-1">App Holdings Value</div>
          <div className="font-semibold text-sm">{formatCurrency(appTotalValue)}</div>
        </div>
        <div className="rounded-lg bg-muted/40 p-3">
          <div className="text-xs text-muted-foreground mb-1">Broker Positions</div>
          <div className="font-semibold text-sm">
            {result ? result.broker_positions ?? '—' : '—'}
          </div>
        </div>
        <div className="rounded-lg bg-muted/40 p-3">
          <div className="text-xs text-muted-foreground mb-1">Matched</div>
          <div className="font-semibold text-sm text-emerald-500">{summary.matched ?? '—'}</div>
        </div>
        <div className="rounded-lg bg-muted/40 p-3">
          <div className="text-xs text-muted-foreground mb-1">Drift / Adjustments</div>
          <div className={`font-semibold text-sm ${hasDrift ? 'text-amber-500' : 'text-emerald-500'}`}>
            {result ? (summary.qty_drift || 0) + (summary.adjustments || 0) : '—'}
          </div>
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div className="flex items-start gap-2 rounded-lg bg-red-500/10 border border-red-500/30 p-3 mb-4">
          <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
          <div>
            <div className="text-sm font-medium text-red-500">Verification Failed</div>
            <div className="text-xs text-muted-foreground mt-0.5">{error}</div>
            <div className="text-xs text-muted-foreground mt-1">
              Make sure your Alpaca API keys are saved in Settings and the broker is connected.
            </div>
          </div>
        </div>
      )}

      {/* Result status banner */}
      <AnimatePresence>
        {result && !error && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div
              className={`flex items-start gap-2 rounded-lg p-3 mb-4 border ${
                allMatch
                  ? 'bg-emerald-500/10 border-emerald-500/30'
                  : 'bg-amber-500/10 border-amber-500/30'
              }`}
            >
              {allMatch ? (
                <CheckCircle2 className="w-4 h-4 text-emerald-500 flex-shrink-0 mt-0.5" />
              ) : (
                <AlertTriangle className="w-4 h-4 text-amber-500 flex-shrink-0 mt-0.5" />
              )}
              <div>
                <div className={`text-sm font-medium ${allMatch ? 'text-emerald-500' : 'text-amber-500'}`}>
                  {allMatch ? 'All positions match broker' : 'Drift detected — positions adjusted'}
                </div>
                <div className="text-xs text-muted-foreground mt-0.5">
                  {summary.matched || 0} matched · {summary.qty_drift || 0} qty drift ·{' '}
                  {summary.price_drift || 0} price drift · {summary.new_from_broker || 0} new from broker ·{' '}
                  {summary.externally_closed || 0} externally closed · {summary.adjustments || 0} adjustments
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Recent reconciliation events */}
      {events.length > 0 && (
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wide">
            Recent Reconciliation Events
          </div>
          <div className="space-y-1.5 max-h-48 overflow-y-auto scrollbar-thin">
            {events.map((e) => {
              const isDrift = ['qty_drift', 'price_drift', 'reconciliation_adjustment'].includes(e.event_type);
              const isMatch = e.event_type === 'matched';
              const isNew = e.event_type === 'new_from_broker';
              const isClosed = e.event_type === 'externally_closed';
              return (
                <div
                  key={e.id}
                  className="flex items-center gap-2 text-xs p-2 rounded-lg bg-muted/30"
                >
                  {isMatch ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />
                  ) : isDrift ? (
                    <AlertTriangle className="w-3.5 h-3.5 text-amber-500 flex-shrink-0" />
                  ) : isNew ? (
                    <ArrowRight className="w-3.5 h-3.5 text-blue-500 flex-shrink-0" />
                  ) : isClosed ? (
                    <XCircle className="w-3.5 h-3.5 text-red-500 flex-shrink-0" />
                  ) : (
                    <AlertTriangle className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                  )}
                  <span className="font-medium">{e.symbol}</span>
                  <span className="text-muted-foreground">
                    {e.event_type.replace(/_/g, ' ')}
                  </span>
                  {e.app_qty != null && e.broker_qty != null && (
                    <span className="text-muted-foreground">
                      {e.app_qty} → {e.broker_qty}
                    </span>
                  )}
                  <span className="text-muted-foreground ml-auto">
                    {new Date(e.run_timestamp).toLocaleString('en-US', {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </motion.div>
  );
}