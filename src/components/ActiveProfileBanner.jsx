import React from 'react';
import { motion } from 'framer-motion';
import { Gauge, TrendingUp, Shield, Layers } from 'lucide-react';
import { cn } from '@/lib/utils';

const RISK_BADGE = {
  High: 'bg-red-500/10 text-red-400 border-red-500/30',
  Medium: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  Low: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
};

export default function ActiveProfileBanner({ profile, filteredCount = 0 }) {
  if (!profile) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-4 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <Gauge className="w-4 h-4 text-accent" />
          <span className="font-semibold text-sm">{profile.name} Profile</span>
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full border',
              RISK_BADGE[profile.risk_level]
            )}
          >
            {profile.risk_level} Risk
          </span>
        </div>
        <div className="flex gap-4 text-xs text-muted-foreground flex-wrap">
          <span className="flex items-center gap-1">
            <Layers className="w-3 h-3" /> Max Pos:{' '}
            <span className="text-foreground font-medium">{profile.max_position_pct}%</span>
          </span>
          <span className="flex items-center gap-1">
            <TrendingUp className="w-3 h-3" /> Max Sector:{' '}
            <span className="text-foreground font-medium">{profile.max_sector_pct}%</span>
          </span>
          <span className="flex items-center gap-1">
            <Shield className="w-3 h-3" /> Min Conf:{' '}
            <span className="text-foreground font-medium">{profile.min_confidence}%</span>
          </span>
          <span>
            Max Trades: <span className="text-foreground font-medium">{profile.max_daily_trades}</span>
          </span>
        </div>
      </div>
      {filteredCount > 0 && (
        <p className="text-xs text-amber-400 mt-2">
          {filteredCount} proposal{filteredCount > 1 ? 's' : ''} filtered out — below{' '}
          {profile.min_confidence}% confidence threshold.
        </p>
      )}
    </motion.div>
  );
}