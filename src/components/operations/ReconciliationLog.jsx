import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Loader2, RefreshCw } from 'lucide-react';
import { base44 } from '@/api/base44Client';

const EVENT_STYLE = {
  matched: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
  qty_drift: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  price_drift: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  externally_closed: 'text-sky-400 bg-sky-500/10 border-sky-500/20',
  new_from_broker: 'text-violet-400 bg-violet-500/10 border-violet-500/20',
  broker_unreachable: 'text-red-400 bg-red-500/10 border-red-500/20',
};

function fmt(n) { return Number(n || 0).toFixed(2); }

export default function ReconciliationLog() {
  const [events, setEvents] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const res = await base44.entities.ReconciliationEvent.list('-created_date', 30);
      setEvents(res);
    } catch (e) {
      setError(e.message || 'Failed to load reconciliation log');
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="rounded-2xl border border-border bg-card p-8 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;
  if (error) return <div className="rounded-2xl border border-border bg-card p-5 text-red-400 text-sm">{error}</div>;
  if (!events || events.length === 0) return <div className="rounded-2xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">No reconciliation events yet — broker sync drift will appear here.</div>;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden">
      <div className="p-5 border-b border-border">
        <h3 className="font-semibold flex items-center gap-2"><RefreshCw className="w-4 h-4 text-accent" /> Broker Reconciliation Log</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground text-xs border-b border-border">
              <th className="text-left font-medium p-3">Event</th>
              <th className="text-left font-medium p-3">Symbol</th>
              <th className="text-right font-medium p-3 hidden md:table-cell">App Qty</th>
              <th className="text-right font-medium p-3 hidden md:table-cell">Broker Qty</th>
              <th className="text-right font-medium p-3 hidden lg:table-cell">App Price</th>
              <th className="text-right font-medium p-3 hidden lg:table-cell">Broker Price</th>
              <th className="text-left font-medium p-3 hidden md:table-cell">Action</th>
              <th className="text-right font-medium p-3 hidden lg:table-cell">P&L</th>
              <th className="text-right font-medium p-3">When</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id} className="border-b border-border/30 hover:bg-muted/20 transition-colors">
                <td className="p-3"><span className={`text-xs px-2 py-0.5 rounded border ${EVENT_STYLE[e.event_type] || 'text-muted-foreground bg-muted/40 border-border'}`}>{e.event_type}</span></td>
                <td className="p-3 font-medium">{e.symbol}</td>
                <td className="text-right p-3 hidden md:table-cell font-mono text-xs">{fmt(e.app_qty)}</td>
                <td className="text-right p-3 hidden md:table-cell font-mono text-xs">{fmt(e.broker_qty)}</td>
                <td className="text-right p-3 hidden lg:table-cell font-mono text-xs">{e.app_avg_price ? `$${fmt(e.app_avg_price)}` : '—'}</td>
                <td className="text-right p-3 hidden lg:table-cell font-mono text-xs">{e.broker_avg_price ? `$${fmt(e.broker_avg_price)}` : '—'}</td>
                <td className="p-3 hidden md:table-cell text-xs text-muted-foreground">{e.action_taken || '—'}</td>
                <td className="text-right p-3 hidden lg:table-cell font-mono text-xs">{e.realized_pnl != null ? <span className={e.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>${fmt(e.realized_pnl)}</span> : '—'}</td>
                <td className="text-right p-3 text-xs text-muted-foreground">{new Date(e.run_timestamp).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}