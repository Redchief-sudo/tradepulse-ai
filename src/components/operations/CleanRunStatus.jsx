import React, { useEffect, useState } from 'react';
import { base44 } from '@/api/base44Client';

const fmtTime = (value) => value ? new Date(value).toLocaleString() : '—';

export default function CleanRunStatus() {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    const load = async () => {
      try {
        const [user, runs, settlements, reconciliation, audits] = await Promise.all([
          base44.auth.me(),
          base44.entities.ScanRun.list('-started_at', 1),
          base44.entities.SettlementEvent.list('-occurred_at', 200),
          base44.entities.ReconciliationEvent.list('-run_timestamp', 20),
          base44.entities.AuditEvent.list('-created_date', 200),
        ]);
        const latest = runs?.[0] || null;
        const unresolved = (settlements || []).filter((event) => event.status !== 'completed' || event.integrity_verified !== true);
        const providerFailures = (audits || []).filter((event) => /provider|market_data|snapshot_capture_failed/.test(String(event.event_type)));
        const healthFailures = (audits || []).filter((event) => String(event.event_type).startsWith('health_check_') && ['warning', 'error', 'critical'].includes(event.severity));
        const next = new Date(); next.setSeconds(0, 0); next.setMinutes(next.getMinutes() + (15 - next.getMinutes() % 15));
        setState({ user, latest, unresolved, providerFailures, healthFailures, reconciliation: reconciliation || [], next });
        setError(null);
      } catch (e) { setError(e.message || 'Clean-run status unavailable'); }
    };
    load(); const timer = setInterval(load, 30000); return () => clearInterval(timer);
  }, []);
  if (error) return <div className="rounded-2xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">Health unknown: {error}</div>;
  if (!state) return null;
  const { user, latest, unresolved, providerFailures, healthFailures, reconciliation, next } = state;
  const dispositions = (() => { try { return JSON.parse(latest?.candidate_dispositions || '[]'); } catch { return []; } })();
  const integrity = user?.trading_session_state === 'financial_integrity_blocked' || unresolved.length ? 'blocked/unresolved' : 'healthy';
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <h2 className="font-semibold mb-3">Clean-Run Truth</h2>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <div><div className="text-xs text-muted-foreground">Trading session</div><div>{user?.trading_session_state || 'unknown'}</div></div>
        <div><div className="text-xs text-muted-foreground">Latest scan</div><div>{latest?.status || 'none'} · {fmtTime(latest?.completed_at || latest?.last_heartbeat_at)}</div></div>
        <div><div className="text-xs text-muted-foreground">Next scheduled scan</div><div>{user?.trading_active ? fmtTime(next) : 'inactive'}</div></div>
        <div><div className="text-xs text-muted-foreground">Candidates accounted</div><div>{latest?.candidates_accounted || 0}/{latest?.candidates_found || 0}</div></div>
        <div><div className="text-xs text-muted-foreground">Broker fills / settled</div><div>{latest?.accepted_fills || 0}/{latest?.settlement_completed || 0}</div></div>
        <div><div className="text-xs text-muted-foreground">Settlement unresolved</div><div>{unresolved.length}</div></div>
        <div><div className="text-xs text-muted-foreground">Financial integrity</div><div>{integrity}</div></div>
        <div><div className="text-xs text-muted-foreground">Reconciliation events</div><div>{reconciliation.length}</div></div>
        <div><div className="text-xs text-muted-foreground">Health failures</div><div>{healthFailures.length}</div></div>
        <div><div className="text-xs text-muted-foreground">Provider degradation</div><div>{providerFailures.length}</div></div>
      </div>
      {dispositions.length > 0 && <div className="mt-3 text-xs text-muted-foreground break-words">Candidate dispositions: {dispositions.map((entry) => `${entry.symbol}=${entry.disposition}`).join(' · ')}</div>}
      {latest?.no_trade_reason && <div className="mt-2 text-xs text-amber-500 break-words">{latest.no_trade_reason}</div>}
    </div>
  );
}
