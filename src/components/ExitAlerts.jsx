import React from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AlertTriangle, Target, ShieldX, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { formatCurrency } from '@/lib/portfolio';
import { cn } from '@/lib/utils';

export default function ExitAlerts({ holdings, onExit }) {
  const alerts = holdings.filter(
    (h) =>
      h.current_price &&
      ((h.target_price > 0 && h.current_price >= h.target_price) ||
        (h.stop_loss > 0 && h.current_price <= h.stop_loss))
  );

  if (alerts.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-amber-500/30 bg-amber-500/5 overflow-hidden mb-8"
    >
      <div className="p-5 border-b border-amber-500/20">
        <div className="flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-400" />
          <h3 className="font-semibold">Exit Alerts</h3>
          <span className="text-xs text-amber-400 ml-1">
            {alerts.length} position{alerts.length > 1 ? 's' : ''} hit exit targets
          </span>
        </div>
        <p className="text-xs text-muted-foreground mt-1.5">
          The AI auto-exits these every 5 minutes during market hours — no action needed.
          Use "Execute Exit" only for an immediate manual exit.
        </p>
      </div>
      <div className="divide-y divide-amber-500/10">
        <AnimatePresence>
          {alerts.map((h) => {
            const isTakeProfit = h.target_price > 0 && h.current_price >= h.target_price;
            const gain =
              h.avg_price > 0
                ? ((h.current_price - h.avg_price) / h.avg_price) * 100
                : 0;
            return (
              <motion.div
                key={h.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="flex items-center justify-between p-4 gap-3 flex-wrap"
              >
                <div className="flex items-center gap-3">
                  <div
                    className={cn(
                      'w-9 h-9 rounded-full flex items-center justify-center',
                      isTakeProfit
                        ? 'bg-emerald-500/10 text-emerald-500'
                        : 'bg-red-500/10 text-red-500'
                    )}
                  >
                    {isTakeProfit ? <Target className="w-4 h-4" /> : <ShieldX className="w-4 h-4" />}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-sm">{h.symbol}</span>
                      <span
                        className={cn(
                          'text-xs font-medium px-2 py-0.5 rounded-full',
                          isTakeProfit
                            ? 'bg-emerald-500/15 text-emerald-400'
                            : 'bg-red-500/15 text-red-400'
                        )}
                      >
                        {isTakeProfit ? 'Take Profit' : 'Stop Loss Hit'}
                      </span>
                    </div>
                    <div className="text-xs text-muted-foreground mt-0.5">
                      Current {formatCurrency(h.current_price)} ·{' '}
                      {isTakeProfit
                        ? `Target ${formatCurrency(h.target_price)}`
                        : `Stop ${formatCurrency(h.stop_loss)}`}{' '}
                      · {gain >= 0 ? '+' : ''}
                      {gain.toFixed(1)}%
                    </div>
                  </div>
                </div>
                <Button
                  size="sm"
                  variant={isTakeProfit ? 'default' : 'destructive'}
                  onClick={() => onExit(h)}
                  className="gap-1.5"
                >
                  <X className="w-3.5 h-3.5" />
                  Execute Exit
                </Button>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}