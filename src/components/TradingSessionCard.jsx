import React from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function TradingSessionCard({ session, isSelected, onClick }) {
  const dailyReturn = session.daily_return_pct;
  const isPositive = dailyReturn != null && dailyReturn >= 0;
  const winRate = session.win_rate_pct || 0;

  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full text-left p-4 rounded-xl border transition-all duration-200',
        isSelected
          ? 'border-primary bg-primary/5 glow-primary'
          : 'border-border bg-card hover:border-primary/40 hover:bg-muted/30'
      )}
    >
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="font-heading font-semibold text-sm">
            {new Date(session.session_date).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {session.status === 'closed' ? 'Final Report' : 'Intraday Snapshot'}
          </div>
        </div>
        <div className={cn(
          'flex items-center gap-1 px-2.5 py-1 rounded-lg text-sm font-bold',
          dailyReturn == null ? 'bg-muted text-muted-foreground' : isPositive ? 'bg-primary/10 text-primary' : 'bg-destructive/10 text-destructive'
        )}>
          {dailyReturn != null && (isPositive ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />)}
          {dailyReturn == null ? 'Unavailable' : `${isPositive ? '+' : ''}${dailyReturn.toFixed(2)}%`}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="text-muted-foreground">Realized P&L</div>
          <div className={cn('font-semibold mt-0.5', (session.realized_pnl || 0) >= 0 ? 'text-primary' : 'text-destructive')}>
            ${(session.realized_pnl || 0).toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground">Win Rate</div>
          <div className="font-semibold mt-0.5">{winRate.toFixed(0)}%</div>
        </div>
        <div>
          <div className="text-muted-foreground">Trades</div>
          <div className="font-semibold mt-0.5">{session.trades_filled || 0}</div>
        </div>
      </div>

      {(session.num_kill_switch_events > 0 || session.num_risk_events > 0) && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          {session.num_kill_switch_events > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-destructive/10 text-destructive font-medium">
              {session.num_kill_switch_events} kill switch
            </span>
          )}
          {session.num_risk_events > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-accent/10 text-accent font-medium">
              {session.num_risk_events} risk events
            </span>
          )}
        </div>
      )}
    </button>
  );
}
