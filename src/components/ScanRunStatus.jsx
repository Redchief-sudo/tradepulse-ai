import React, { useState, useEffect, useRef } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Activity, CheckCircle2, AlertTriangle, Clock, XCircle, Loader2, Radar } from 'lucide-react';
import { scannerActivity } from '@/lib/scanActivity';

function timeAgo(date) {
  if (!date) return '';
  const diff = Date.now() - new Date(date).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

const STATUS_TONE = {
  running: { icon: Activity, color: 'text-accent', label: 'Running' },
  completed: { icon: CheckCircle2, color: 'text-emerald-500', label: 'Completed' },
  failed: { icon: AlertTriangle, color: 'text-red-500', label: 'Failed' },
  no_candidates: { icon: XCircle, color: 'text-muted-foreground', label: 'No candidates' },
  broker_unavailable: { icon: AlertTriangle, color: 'text-amber-500', label: 'Broker unavailable' },
};

// Displays the most recent persisted scan-cycle records so the Dashboard
// reflects scan state (last run, candidates, proposals, vetoes, trades)
// from authoritative data rather than transient page state.
export default function ScanRunStatus() {
  const [runs, setRuns] = useState([]);
  const [tradingState, setTradingState] = useState({ active: false, session: 'disabled' });
  const [pendingRequests, setPendingRequests] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const loadingRef = useRef(false);

  const load = async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try {
      const [r, requests, user] = await Promise.all([
        base44.entities.ScanRun.list('-started_at', 5),
        base44.entities.ScanRequest.list('-requested_at', 20),
        base44.auth.me(),
      ]);
      setRuns(r || []);
      setPendingRequests((requests || []).filter((request) => ['pending', 'processing'].includes(request.status)).length);
      setTradingState({ active: Boolean(user?.trading_active), session: user?.trading_session_state || 'disabled' });
      setLoadError(null);
    } catch (e) {
      setLoadError(e?.message || 'Unable to verify scanner activity');
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    const onVisible = () => { if (document.visibilityState === 'visible') load(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);

  if (loading) return null;

  const latest = runs[0];
  const activity = scannerActivity({
    tradingActive: tradingState.active,
    sessionState: tradingState.session,
    latestRun: latest,
    pendingRequests,
  });
  const tone = latest ? (STATUS_TONE[latest.status] || STATUS_TONE.completed) : STATUS_TONE.no_candidates;
  const Icon = tone.icon;
  const live = activity.state === 'scanning';
  const warning = activity.state === 'stale' || Boolean(loadError);

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`rounded-2xl border p-5 mb-6 ${live ? 'border-accent/50 bg-accent/5 shadow-[0_0_24px_rgba(139,92,246,0.12)]' : warning ? 'border-amber-500/40 bg-amber-500/5' : 'border-border bg-card'}`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          {live ? <Radar className="w-5 h-5 text-accent animate-pulse" /> : <Activity className="w-4 h-4 text-accent" />}
          <h3 className="font-semibold text-sm">AI Opportunity Scanner</h3>
        </div>
        {latest && (
          <span className="text-xs text-muted-foreground flex items-center gap-1">
            <Clock className="w-3 h-3" />
            {timeAgo(latest.completed_at || latest.last_heartbeat_at || latest.started_at)}
          </span>
        )}
      </div>

      <div className="flex items-start gap-3 mb-4">
        {live ? <Loader2 className="w-5 h-5 mt-0.5 text-accent animate-spin" /> : <Icon className={`w-5 h-5 mt-0.5 ${warning ? 'text-amber-500' : tone.color}`} />}
        <div>
          <div className={`text-sm font-semibold ${live ? 'text-accent' : warning ? 'text-amber-500' : ''}`}>{activity.label}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{loadError || activity.detail}</div>
        </div>
        {latest?.market_regime && (
          <span className="text-xs text-muted-foreground ml-auto">
            Regime: <span className="text-foreground">{latest.market_regime}</span>
          </span>
        )}
      </div>

      {live && <div className="h-1.5 rounded-full bg-muted overflow-hidden mb-4"><div className="h-full w-1/3 rounded-full bg-accent animate-pulse" /></div>}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
        <div>
          <div className="text-xs text-muted-foreground">Candidates</div>
          <div className="font-medium">{latest?.candidates_found || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Proposals</div>
          <div className="font-medium">{latest?.proposals_created || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Vetoed</div>
          <div className="font-medium">{latest?.proposals_vetoed || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Trades filled</div>
          <div className="font-medium text-emerald-500">{latest?.trades_filled || 0}</div>
        </div>
      </div>

      {latest?.error && (
        <p className="text-xs text-red-500 mt-3 break-words">{latest.error}</p>
      )}
    </motion.div>
  );
}
