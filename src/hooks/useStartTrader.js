import { useState, useCallback } from 'react';
import { base44 } from '@/api/base44Client';

// Reusable hook that initiates an autonomous scan via the Scan Coordinator.
//
// ARCHITECTURE: Manual UI actions do NOT call runAutonomousScanCycle directly.
// They create a ScanRequest, then invoke the Scan Coordinator — the single
// entry point that serializes scan execution. This prevents browser requests
// and scheduled workflows from competing to create scans.
//
// The coordinator picks up the pending ScanRequest and runs the 5-pass AI scan.
export function useStartTrader() {
  const [isRunning, setIsRunning] = useState(false);
  const [stageLabel, setStageLabel] = useState(null);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const startTrader = useCallback(async ({ onComplete } = {}) => {
    setIsRunning(true);
    setError(null);
    setResult(null);
    setStageLabel('Creating scan request');

    try {
      // Create a ScanRequest — the coordinator consumes it.
      await base44.entities.ScanRequest.create({
        status: 'pending',
        trigger_source: 'dashboard',
        requested_at: new Date().toISOString(),
      });

      setStageLabel('Running scan coordinator');

      // Invoke the scan coordinator — it picks up the pending request and runs the scan.
      const res = await base44.functions.invoke('runScanCoordinator', {});
      const data = res?.data || res;

      if (!data || data.ok === false || data.error) {
        throw new Error(data?.error || 'Scan coordinator failed');
      }

      // The coordinator returns the scan result
      const scanData = data.result || data;
      const executedList = scanData?.executed || [];
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
        marketSummary: scanData?.market_summary,
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