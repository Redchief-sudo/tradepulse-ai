import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Loader2, Database } from 'lucide-react';
import { base44 } from '@/api/base44Client';

function ageMinutes(iso) {
  if (!iso) return null;
  return Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
}

export default function DataFreshness() {
  const [snapshots, setSnapshots] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const res = await base44.entities.PriceSnapshot.list('-created_date', 200);
      setSnapshots(res);
    } catch (e) {
      setError(e.message || 'Failed to load snapshots');
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  if (loading) return <div className="rounded-2xl border border-border bg-card p-8 flex justify-center"><Loader2 className="w-5 h-5 animate-spin text-primary" /></div>;
  if (error) return <div className="rounded-2xl border border-border bg-card p-5 text-red-400 text-sm">{error}</div>;
  if (!snapshots) return null;

  const latestBySymbol = {};
  for (const s of snapshots) {
    const sym = s.symbol;
    if (!latestBySymbol[sym] || new Date(s.timestamp) > new Date(latestBySymbol[sym].timestamp)) {
      latestBySymbol[sym] = s;
    }
  }
  const rows = Object.values(latestBySymbol).sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  const maxAge = rows.length ? Math.max(...rows.map((r) => ageMinutes(r.timestamp) || 0)) : 0;
  const fresh = maxAge <= 15;
  const stale = maxAge > 60;

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border border-border bg-card overflow-hidden">
      <div className="p-5 border-b border-border flex items-center justify-between">
        <h3 className="font-semibold flex items-center gap-2"><Database className="w-4 h-4 text-chart-3" /> Price Snapshot Freshness</h3>
        <div className="flex items-center gap-2">
          <span className={`text-xs px-2 py-1 rounded border ${fresh ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' : stale ? 'text-red-400 bg-red-500/10 border-red-500/20' : 'text-amber-400 bg-amber-500/10 border-amber-500/20'}`}>
            {fresh ? 'FRESH' : stale ? 'STALE' : 'AGING'} · max {maxAge}m
          </span>
          <span className="text-xs text-muted-foreground">{rows.length} symbols</span>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="p-8 text-center text-sm text-muted-foreground">No price snapshots captured yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-muted-foreground text-xs border-b border-border">
                <th className="text-left font-medium p-3">Symbol</th>
                <th className="text-right font-medium p-3">Price</th>
                <th className="text-left font-medium p-3">Source</th>
                <th className="text-right font-medium p-3">Age</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const age = ageMinutes(r.timestamp);
                return (
                  <tr key={r.id} className="border-b border-border/30 hover:bg-muted/20 transition-colors">
                    <td className="p-3 font-medium">{r.symbol}</td>
                    <td className="text-right p-3 font-mono text-xs">${Number(r.price).toFixed(2)}</td>
                    <td className="p-3 text-xs text-muted-foreground">{r.source || '—'}</td>
                    <td className="text-right p-3 text-xs">
                      <span className={age <= 15 ? 'text-emerald-400' : age > 60 ? 'text-red-400' : 'text-amber-400'}>{age != null ? `${age}m` : '—'}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </motion.div>
  );
}