import React, { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import {
  ShieldAlert,
  RefreshCw,
  Loader2,
  TrendingDown,
  AlertTriangle,
  CheckCircle2,
  Percent,
  Activity,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { formatCurrency } from '@/lib/portfolio';

export default function StopLossScanner({ holdings, stopLossPct, onStopLossPctChange, onHoldingsChange }) {
  const [scanning, setScanning] = useState(false);
  const [hasScanned, setHasScanned] = useState(false);
  const [triggered, setTriggered] = useState([]);
  const [atRisk, setAtRisk] = useState([]);
  const [localPct, setLocalPct] = useState(stopLossPct || 8);

  useEffect(() => {
    setLocalPct(stopLossPct || 8);
  }, [stopLossPct]);

  // Compute at-risk + triggered from stored prices (instant, no API call)
  useEffect(() => {
    const trig = [];
    const risk = [];
    holdings.forEach((h) => {
      const current = h.current_price || h.avg_price;
      const dropPct =
        h.avg_price > 0 ? ((h.avg_price - current) / h.avg_price) * 100 : 0;
      if (dropPct >= localPct) {
        trig.push({ ...h, dropPct });
      } else if (dropPct >= localPct - 3 && dropPct > 0) {
        risk.push({ ...h, dropPct });
      }
    });
    setTriggered(trig);
    setAtRisk(risk);
  }, [holdings, localPct]);

  const updatePct = (val) => {
    const num = Math.max(1, Math.min(50, Number(val) || 8));
    setLocalPct(num);
    onStopLossPctChange(num);
  };

  const runScan = async () => {
    if (holdings.length === 0) return;
    setScanning(true);
    setTriggered([]);
    setHasScanned(false);
    try {
      // 1. Refresh live prices
      const symbols = holdings.map((h) => h.symbol);
      const result = await base44.functions.invoke('marketData', { symbols });
      const quotes = (result.data?.quotes || []).filter((q) => !q.error);
      const priceMap = {};
      quotes.forEach((q) => {
        priceMap[q.symbol.toUpperCase()] = q.current_price;
      });

      // Update stored prices
      const updates = holdings
        .filter((h) => priceMap[h.symbol])
        .map((h) => ({ id: h.id, current_price: priceMap[h.symbol] }));
      if (updates.length) {
        await base44.entities.Holding.bulkUpdate(updates);
      }

      // 2. Check stop-loss with live prices and auto-execute
      const executed = [];
      const stillAtRisk = [];
      for (const h of holdings) {
        const livePrice = priceMap[h.symbol] || h.current_price || h.avg_price;
        const dropPct =
          h.avg_price > 0 ? ((h.avg_price - livePrice) / h.avg_price) * 100 : 0;
        if (dropPct >= localPct) {
          try {
            const result = await base44.functions.invoke('executeTrade', {
              symbol: h.symbol,
              action: 'sell',
              qty: h.shares,
              price: livePrice,
              company_name: h.company_name,
              ai_recommended: true,
              source: 'stoploss_ui',
              notes: `Auto stop-loss triggered at -${dropPct.toFixed(1)}% (threshold: ${localPct}%)`,
            });
            if (result?.data?.status === 'filled' || result?.data?.status === 'paper_filled') {
              executed.push({ ...h, current_price: livePrice, dropPct });
            }
          } catch (e) {
            console.error('Stop-loss execution failed:', e);
          }
        } else if (dropPct >= localPct - 3 && dropPct > 0) {
          stillAtRisk.push({ ...h, current_price: livePrice, dropPct });
        }
      }
      setTriggered(executed);
      setAtRisk(stillAtRisk);
      setHasScanned(true);
      await onHoldingsChange();
    } catch (e) {
      console.error(e);
    }
    setScanning(false);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card overflow-hidden mb-6"
    >
      <div className="p-5 border-b border-border flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <ShieldAlert className="w-4 h-4 text-amber-400" />
          <h3 className="font-semibold">Auto Stop-Loss Scanner</h3>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-sm text-muted-foreground flex items-center gap-1.5">
            <Percent className="w-3.5 h-3.5" />
            Threshold
          </label>
          <div className="flex items-center gap-1">
            <input
              type="number"
              min="1"
              max="50"
              step="0.5"
              value={localPct}
              onChange={(e) => setLocalPct(e.target.value)}
              onBlur={(e) => updatePct(e.target.value)}
              className="w-16 h-8 rounded-md border border-input bg-transparent px-2 text-sm text-center"
            />
            <span className="text-sm text-muted-foreground">%</span>
          </div>
          <Button
            size="sm"
            onClick={runScan}
            disabled={scanning || holdings.length === 0}
            variant="secondary"
            className="gap-1.5 ml-1"
          >
            {scanning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            {scanning ? 'Scanning...' : 'Refresh & Execute'}
          </Button>
        </div>
      </div>

      <div className="p-4 space-y-3">
        <p className="text-xs text-muted-foreground">
          Automatically sells any position that drops {localPct}% below your average buy price. Positions
          within 3% of the threshold are flagged as "at risk."
        </p>

        {/* Triggered (auto-exited) */}
        <AnimatePresence>
          {triggered.length > 0 && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-3">
                <div className="flex items-center gap-2 mb-2">
                  <TrendingDown className="w-4 h-4 text-red-500" />
                  <span className="text-sm font-medium text-red-500">
                    {hasScanned
                      ? `${triggered.length} Auto-Exited`
                      : `${triggered.length} Position${triggered.length > 1 ? 's' : ''} Below ${localPct}% Threshold`}
                  </span>
                </div>
                <div className="space-y-1.5">
                  {triggered.map((h) => (
                    <div
                      key={h.id}
                      className="flex items-center justify-between text-sm flex-wrap gap-1"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{h.symbol}</span>
                        <span className="text-xs text-muted-foreground">
                          {h.shares} @ {formatCurrency(h.avg_price)}
                        </span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground">
                          Now {formatCurrency(h.current_price)}
                        </span>
                        <span className="text-red-500 font-medium text-xs">
                          -{h.dropPct.toFixed(1)}%
                        </span>
                        {scanning ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin text-red-500" />
                        ) : (
                          <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                        )}
                      </div>
                    </div>
                  ))}
                </div>
                {!scanning && hasScanned ? (
                  <p className="text-xs text-emerald-500 mt-2 flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" />
                    Auto-exited — proceeds recorded as sell trades
                  </p>
                ) : (
                  <p className="text-xs text-amber-400 mt-2 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" />
                    Click "Refresh & Execute" to auto-sell these positions
                  </p>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* At risk */}
        <AnimatePresence>
          {atRisk.length > 0 && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
                <div className="flex items-center gap-2 mb-2">
                  <AlertTriangle className="w-4 h-4 text-amber-400" />
                  <span className="text-sm font-medium text-amber-400">
                    {atRisk.length} At Risk
                  </span>
                </div>
                <div className="space-y-1.5">
                  {atRisk.map((h) => (
                    <div
                      key={h.id}
                      className="flex items-center justify-between text-sm flex-wrap gap-1"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{h.symbol}</span>
                        <span className="text-xs text-muted-foreground">
                          {h.shares} @ {formatCurrency(h.avg_price)}
                        </span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground">
                          Now {formatCurrency(h.current_price)}
                        </span>
                        <span className="text-amber-400 font-medium text-xs">
                          -{h.dropPct.toFixed(1)}%
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* All clear */}
        {triggered.length === 0 && atRisk.length === 0 && holdings.length > 0 && (
          <div className="flex items-center gap-2 text-sm text-emerald-500 py-1">
            <CheckCircle2 className="w-4 h-4" />
            All positions are within safe range.
          </div>
        )}

        {holdings.length === 0 && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground py-1">
            <Activity className="w-4 h-4" />
            No holdings to monitor. Add positions on the Dashboard to enable stop-loss protection.
          </div>
        )}
      </div>
    </motion.div>
  );
}