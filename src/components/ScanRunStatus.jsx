import React, { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Activity, CheckCircle2, AlertTriangle, Clock, XCircle } from 'lucide-react';

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
  const [loading, setLoading] = useState(true);

  const load = async () => {
    try {
      const r = await base44.entities.ScanRun.list('-started_at', 5);
      setRuns(r || []);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, []);

  if (loading || runs.length === 0) return null;

  const latest = runs[0];
  const tone = STATUS_TONE[latest.status] || STATUS_TONE.completed;
  const Icon = tone.icon;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-5 mb-6"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-accent" />
          <h3 className="font-semibold text-sm">Last AI Scan</h3>
        </div>
        <span className="text-xs text-muted-foreground flex items-center gap-1">
          <Clock className="w-3 h-3" />
          {timeAgo(latest.completed_at || latest.started_at)}
        </span>
      </div>

      <div className="flex items-center gap-2 mb-3">
        <Icon className={`w-4 h-4 ${tone.color}`} />
        <span className={`text-sm font-medium ${tone.color}`}>{tone.label}</span>
        {latest.market_regime && (
          <span className="text-xs text-muted-foreground ml-auto">
            Regime: <span className="text-foreground">{latest.market_regime}</span>
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
        <div>
          <div className="text-xs text-muted-foreground">Candidates</div>
          <div className="font-medium">{latest.candidates_found || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Proposals</div>
          <div className="font-medium">{latest.proposals_created || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Vetoed</div>
          <div className="font-medium">{latest.proposals_vetoed || 0}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Trades filled</div>
          <div className="font-medium text-emerald-500">{latest.trades_filled || 0}</div>
        </div>
      </div>

      {latest.error && (
        <p className="text-xs text-red-500 mt-3 break-words">{latest.error}</p>
      )}
    </motion.div>
  );
}