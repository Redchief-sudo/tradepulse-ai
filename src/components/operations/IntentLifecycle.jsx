import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Loader2, GitBranch } from 'lucide-react';
import { base44 } from '@/api/base44Client';

const STATUS_STYLE = {
  proposed: 'text-sky-400 bg-sky-500/10 border-sky-500/20',
  risk_approved: 'text-sky-400 bg-sky-500/10 border-sky-500/20',
  submitted: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  accepted: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  partially_filled: 'text-violet-400 bg-violet-500/10 border-violet-500/20',
  filled: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
  settled: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
  rejected: 'text-red-400 bg-red-500/10 border-red-500/20',
  canceled: 'text-red-400 bg-red-500/10 border-red-500/20',
  expired: 'text-red-400 bg-red-500/10 border-red-500/20',
  failed: 'text-red-400 bg-red-500/10 border-red-500/20',
};

function timeAgo(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function IntentLifecycle() {
  const [intents, setIntents] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const res = await base44.entities.TradeIntent.list('-created_date', 50);
      setIntents(res);
    } catch (e) {
      setError(e.message || 'Failed to load intents');
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="rounded-2xl border border-border bg-card p-8 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;
  if (error) return <div className="rounded-2xl border border-border bg-card p-5 text-red-400 text-sm">{error}</div>;
  if (!intents || intents.length === 0) return <div className="rounded-2xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">No trade intents yet — the execution pipeline state machine will appear here.</div>;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden">
      <div className="p-5 border-b border-border">
        <h3 className="font-semibold flex items-center gap-2"><GitBranch className="w-4 h-4 text-primary" /> Execution Pipeline — Recent Intents</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground text-xs border-b border-border">
              <th className="text-left font-medium p-3">Intent</th>
              <th className="text-left font-medium p-3">Symbol</th>
              <th className="text-left font-medium p-3">Side</th>
              <th className="text-right font-medium p-3">Qty</th>
              <th className="text-left font-medium p-3 hidden md:table-cell">Mode</th>
              <th className="text-left font-medium p-3">Status</th>
              <th className="text-left font-medium p-3 hidden lg:table-cell">Reason</th>
              <th className="text-right font-medium p-3">Age</th>
            </tr>
          </thead>
          <tbody>
            {intents.map((i) => (
              <tr key={i.id} className="border-b border-border/30 hover:bg-muted/20 transition-colors">
                <td className="p-3 font-mono text-xs text-muted-foreground">{i.trade_intent_id?.slice(0, 12) || '—'}</td>
                <td className="p-3 font-medium">{i.symbol}</td>
                <td className="p-3"><span className={i.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{i.side}</span></td>
                <td className="text-right p-3 font-mono text-xs">{i.requested_quantity}</td>
                <td className="p-3 hidden md:table-cell"><span className="text-xs px-2 py-0.5 rounded bg-muted/40 border border-border">{i.execution_mode}</span></td>
                <td className="p-3"><span className={`text-xs px-2 py-0.5 rounded border ${STATUS_STYLE[i.status] || 'text-muted-foreground bg-muted/40 border-border'}`}>{i.status}</span></td>
                <td className="p-3 hidden lg:table-cell text-xs text-muted-foreground truncate max-w-[200px]">{i.rejection_reason || '—'}</td>
                <td className="text-right p-3 text-xs text-muted-foreground">{timeAgo(i.created_date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}