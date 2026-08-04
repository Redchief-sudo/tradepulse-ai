import { useState, useCallback } from 'react';
import { base44 } from '@/api/base44Client';

// Reusable hook that runs the full autonomous cycle via the backend
// runAutonomousScanCycle function — the authoritative 5-pass AI scan +
// auto-execute pipeline. This sizes positions from real broker account
// equity (not stale holdings cache), persists ScanRun records, uses the
// champion StrategyModel weights, and routes every trade through the
// canonical execution gateway with lot-based accounting.
//
// Used by the Dashboard "Start Trader" button.
export function useStartTrader() {
  const [isRunning, setIsRunning] = useState(false);
  const [stageLabel, setStageLabel] = useState(null);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const startTrader = useCallback(async ({ onComplete } = {}) => {
    setIsRunning(true);
    setError(null);
    setResult(null);
    setStageLabel('Running full AI scan + auto-execution');

    try {
      const res = await base44.functions.invoke('runAutonomousScanCycle', { trigger_source: 'dashboard' });
      const data = res?.data || res;

      if (!data || data.ok === false || data.error) {
        throw new Error(data?.error || 'Scan cycle failed');
      }

      // The executed array includes rejected attempts too. Count only
      // actually-filled trades (filled / paper_filled) so the UI reflects
      // real portfolio changes.
      const executedList = data.executed || [];
      const filled = executedList.filter(
        (e) =>
          e.settlement?.status === 'filled' ||
          e.settlement?.status === 'paper_filled'
      );

      const proposals = filled.map((e) => ({
        symbol: e.symbol,
        action: e.action,
        qty: e.qty,
        price: e.price,
        ml_score: e.ml_score,
      }));

      setResult({
        proposals,
        executed: filled.length,
        attempted: executedList.length,
        marketSummary: data.market_summary,
      });

      if (onComplete) {
        try {
          await onComplete({ proposals, executed: filled.length });
        } catch (e) {
          console.error(e);
        }
      }
      return { proposals, executed: filled.length };
    } catch (e) {
      console.error(e);
      const msg = e?.message || String(e) || 'Scan failed';
      setError(msg);
      return { proposals: [], executed: 0, error: msg };
    } finally {
      setIsRunning(false);
      setStageLabel(null);
    }
  }, []);

  return { isRunning, stageLabel, error, result, startTrader };
}