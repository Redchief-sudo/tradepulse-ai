// Scan Coordinator — the SINGLE ENTRY POINT for initiating autonomous scans.
//
// Only this function (called by the scheduled workflow) may run scans.
// Manual UI actions do NOT call runAutonomousScanCycle directly — they create
// a ScanRequest, and this coordinator consumes it.
//
// This serializes scan execution: no browser request and scheduled workflow
// compete to create scans. The ScanLock remains diagnostic only — it is NOT
// the primary correctness guarantee. The coordinator IS the serialization
// boundary.
//
// Flow:
// 1. Check for pending ScanRequests (manual triggers from dashboard)
// 2. If found, claim the oldest, run the scan, mark completed/failed
// 3. If no manual requests and trading is active, run a scheduled scan
// 4. If no requests and trading not active, skip

import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { isScheduledScanDue, isSuccessfulScanTerminal } from '../../shared/scanState.ts';

const STALE_LEASE_MS = 5 * 60 * 1000; // 5 minutes

function nowIso() { return new Date().toISOString(); }

async function verifyPersistedScanCompletion(sr, userId, data) {
  if (!data?.scan_run_id) throw new Error('SCAN_RUN_ID_MISSING');
  const runs = await sr.entities.ScanRun.filter({ user_id: userId, scan_run_id: data.scan_run_id });
  const run = runs[0];
  if (!run) throw new Error(`SCAN_RUN_NOT_FOUND: ${data.scan_run_id}`);
  if (!isSuccessfulScanTerminal(run)) {
    throw new Error(`SCAN_RUN_NOT_TERMINAL_SUCCESS: ${run.status}${run.error ? `: ${run.error}` : ''}`);
  }
  return run;
}

export default async function(req) {
  const base44 = createClientFromRequest(req);
  const user = await base44.auth.me();
  if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

  const sr = base44.asServiceRole;
  const workerId = `coordinator-${crypto.randomUUID()}`;
  const now = Date.now();

  // 1. Check for pending ScanRequests (manual triggers)
  const allRequests = await sr.entities.ScanRequest.filter({ user_id: user.id });
  const pending = allRequests
    .filter((r) =>
      r.status === 'pending' ||
      (r.status === 'processing' && r.processing_started_at &&
        now - new Date(r.processing_started_at).getTime() > STALE_LEASE_MS)
    )
    .sort((a, b) => new Date(a.requested_at) - new Date(b.requested_at));

  if (pending.length > 0) {
    // Claim the oldest request
    const oldest = pending[0];
    await sr.entities.ScanRequest.update(oldest.id, {
      status: 'processing',
      processing_started_at: nowIso(),
      processing_owner: workerId,
    });

    // Re-read to verify claim
    const afterClaim = await sr.entities.ScanRequest.filter({ user_id: user.id });
    const reloaded = afterClaim.find((candidate) => candidate.id === oldest.id);
    if (!reloaded || reloaded.processing_owner !== workerId) {
      return Response.json({ ok: true, skipped: true, reason: 'Lost claim race' });
    }

    try {
      const result = await base44.functions.invoke('runAutonomousScanCycle', {
        trigger_source: oldest.trigger_source,
        scan_request_id: oldest.id,
      });
      const data = result?.data || result;
      if (!data || data.ok === false) throw new Error(data?.error || 'SCAN_RETURNED_UNSUCCESSFUL_RESULT');
      await verifyPersistedScanCompletion(sr, user.id, data);

      await sr.entities.ScanRequest.update(oldest.id, {
        status: 'completed',
        completed_at: nowIso(),
        scan_run_id: data?.scan_run_id || null,
      });

      return Response.json({ ok: true, source: 'manual', result: data });
    } catch (e) {
      await sr.entities.ScanRequest.update(oldest.id, {
        status: 'failed',
        completed_at: nowIso(),
        error: e.message,
      });
      return Response.json({ ok: false, source: 'manual', error: e.message }, { status: 500 });
    }
  }

  // 2. The coordinator polls every minute for manual requests, but autonomous
  // scheduled scans retain the 15-minute cadence.
  const recentRuns = await sr.entities.ScanRun.filter({ user_id: user.id });
  const scheduledSlotDue = isScheduledScanDue(now, recentRuns);
  if (user.trading_active && scheduledSlotDue) {
    try {
      const result = await base44.functions.invoke('runAutonomousScanCycle', {
        trigger_source: 'scheduled',
      });
      const data = result?.data || result;
      if (!data || data.ok === false) throw new Error(data?.error || 'SCAN_RETURNED_UNSUCCESSFUL_RESULT');
      await verifyPersistedScanCompletion(sr, user.id, data);
      return Response.json({ ok: true, source: 'scheduled', result: data });
    } catch (e) {
      return Response.json({ ok: false, source: 'scheduled', error: e.message }, { status: 500 });
    }
  }

  return Response.json({ ok: true, skipped: true, reason: 'No pending request and no scheduled scan due' });
}
