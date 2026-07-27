import React from 'react';
import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';
import { TrendingUp, TrendingDown } from 'lucide-react';

export default function StatCard({ label, value, change, changeLabel, icon: Icon, accent = false }) {
  const isPositive = (change ?? 0) >= 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        'rounded-2xl border p-5 transition-colors',
        accent
          ? 'bg-gradient-to-br from-primary/10 to-accent/5 border-primary/20'
          : 'bg-card border-border'
      )}
    >
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm text-muted-foreground">{label}</span>
        {Icon && (
          <Icon className={cn('w-4 h-4', accent ? 'text-primary' : 'text-muted-foreground')} />
        )}
      </div>
      <div className="text-xl md:text-2xl font-bold font-heading tracking-tight">{value}</div>
      {change !== undefined && (
        <div className="flex items-center gap-1.5 mt-2 text-sm">
          <span
            className={cn(
              'flex items-center gap-0.5 font-medium',
              isPositive ? 'text-emerald-500' : 'text-red-500'
            )}
          >
            {isPositive ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
            {isPositive ? '+' : ''}
            {change.toFixed(2)}%
          </span>
          {changeLabel && <span className="text-muted-foreground">{changeLabel}</span>}
        </div>
      )}
    </motion.div>
  );
}