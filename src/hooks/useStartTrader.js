import { useState, useCallback } from 'react';
import { base44 } from '@/api/base44Client';

// Reusable hook that initiates an autonomous scan via the Scan Coordinator.
//
// ARCHITECTURE: Manual UI actions do NOT call runAutonomousScanCycle directly.
// They only create a ScanRequest. A single scheduled coordinator consumes it;
// browsers never invoke the coordinator and therefore cannot race its workflow.
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
      const me = await base44.auth.me();
      if (!me?.id) throw new Error('Authenticated user identity is required');
      const request = await base44.entities.ScanRequest.create({
        user_id: me.id,
        status: 'pending',
        trigger_source: 'dashboard',
        requested_at: new Date().toISOString(),
      });

      setStageLabel('Scan queued');
      const queued = { queued: true, requestId: request.id, proposals: [], executed: 0 };
      setResult(queued);

      if (onComplete) {
        try {
          await onComplete(queued);
        } catch (e) {
          console.error(e);
        }
      }
      return queued;
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
