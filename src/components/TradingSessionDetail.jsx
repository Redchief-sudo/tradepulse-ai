import React from 'react';
import { TrendingUp, TrendingDown, AlertTriangle, CheckCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

const fmt = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n || 0);
const fmtPct = (n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;

function MetricRow({ label, value, tone }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn('font-semibold', tone === 'positive' && 'text-primary', tone === 'negative' && 'text-destructive')}>
        {value}
      </span>
    </div>
  );
}

export default function TradingSessionDetail({ session, trades, scanRuns, auditEvents }) {
  if (!session) return null;

  const sectorExposure = (() => {
    try { return JSON.parse(session.sector_exposure || '[]'); } catch (e) { return []; }
  })();

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-heading font-bold text-lg">
            {new Date(session.session_date).toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}
          </h3>
          <div className="flex items-center gap-2 mt-1">
            <span className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              session.status === 'closed' ? 'bg-primary/10 text-primary' : 'bg-accent/10 text-accent'
            )}>
              {session.status === 'closed' ? 'Final Report' : 'Intraday Snapshot'}
            </span>
            {session.status === 'closed' && (
              <span className="px-2 py-0.5 rounded text-xs bg-muted text-muted-foreground">
                Immutable historical snapshot
              </span>
            )}
            {session.market_regime && (
              <span className="px-2 py-0.5 rounded text-xs bg-muted text-muted-foreground">
                {session.market_regime}
              </span>
            )}
            {session.model_version && (
              <span className="px-2 py-0.5 rounded text-xs bg-muted text-muted-foreground">
                Model: {session.model_version}
              </span>
            )}
          </div>
        </div>
        <div className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-xl',
          session.daily_return_pct == null ? 'bg-muted text-muted-foreground' : session.daily_return_pct >= 0 ? 'bg-primary/10 text-primary' : 'bg-destructive/10 text-destructive'
        )}>
          {session.daily_return_pct != null && (session.daily_return_pct >= 0 ? <TrendingUp className="w-5 h-5" /> : <TrendingDown className="w-5 h-5" />)}
          <span className="text-2xl font-bold">{session.daily_return_pct == null ? 'Unavailable' : fmtPct(session.daily_return_pct)}</span>
        </div>
      </div>

      {/* Equity & P&L Summary */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="p-4 rounded-xl border border-border bg-card space-y-1">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Equity</h4>
          <MetricRow label="Starting Equity" value={fmt(session.starting_equity)} />
          <MetricRow label="Ending Equity" value={fmt(session.ending_equity)} />
          {session.broker_equity != null && <MetricRow label="Broker Equity" value={fmt(session.broker_equity)} />}
          {session.cash_balance != null && <MetricRow label="Cash Balance" value={fmt(session.cash_balance)} />}
          {session.buying_power != null && <MetricRow label="Buying Power" value={fmt(session.buying_power)} />}
        </div>
        <div className="p-4 rounded-xl border border-border bg-card space-y-1">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">P&L Breakdown</h4>
          <MetricRow label="Realized P&L" value={fmt(session.realized_pnl)} tone={(session.realized_pnl || 0) >= 0 ? 'positive' : 'negative'} />
          <MetricRow label="Unrealized P&L" value={fmt(session.unrealized_pnl)} tone={(session.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'} />
          <MetricRow label="Commissions" value={fmt(session.commissions_total)} tone="negative" />
          <MetricRow label="Fees" value={fmt(session.fees_total)} tone="negative" />
          <MetricRow label="Max Drawdown" value={session.max_drawdown_pct == null ? 'Unavailable' : `${session.max_drawdown_pct.toFixed(2)}%`} tone={session.max_drawdown_pct == null ? undefined : 'negative'} />
        </div>
      </div>

      {/* Trade Statistics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Win Rate</div>
          <div className="text-xl font-bold mt-1">{(session.win_rate_pct || 0).toFixed(0)}%</div>
          <div className="text-xs text-muted-foreground mt-0.5">{session.num_winners || 0}W / {session.num_losers || 0}L</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Avg Winner</div>
          <div className="text-xl font-bold mt-1 text-primary">{fmt(session.avg_winner)}</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Avg Loser</div>
          <div className="text-xl font-bold mt-1 text-destructive">{fmt(session.avg_loser)}</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Largest Loss</div>
          <div className="text-xl font-bold mt-1 text-destructive">{fmt(session.largest_loser)}</div>
        </div>
      </div>

      {/* Activity Counts */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Scans</div>
          <div className="text-lg font-semibold mt-0.5">{session.num_scans || 0}</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">AI Decisions</div>
          <div className="text-lg font-semibold mt-0.5">{session.num_ai_decisions || 0}</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">Trades Filled</div>
          <div className="text-lg font-semibold mt-0.5">{session.trades_filled || 0}</div>
          <div className="text-xs text-muted-foreground">{session.trades_submitted || 0} submitted, {session.trades_rejected || 0} rejected</div>
        </div>
        <div className="p-3 rounded-xl border border-border bg-card">
          <div className="text-xs text-muted-foreground">
            {session.broker_data_status === 'available' ? 'Broker Positions at Snapshot' : 'Ledger Positions at Snapshot'}
          </div>
          <div className="text-lg font-semibold mt-0.5">{session.open_positions || 0}</div>
        </div>
      </div>

      {/* Risk & Reconciliation */}
      <div className="p-4 rounded-xl border border-border bg-card">
        <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Risk & Reconciliation</h4>
        <div className="flex flex-wrap items-center gap-3">
          {session.num_kill_switch_events > 0 ? (
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-destructive/10 text-destructive text-xs font-medium">
              <AlertTriangle className="w-3.5 h-3.5" />
              {session.num_kill_switch_events} kill switch event(s)
            </span>
          ) : (
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
              <CheckCircle className="w-3.5 h-3.5" />
              No kill switch events
            </span>
          )}
          {session.num_risk_events > 0 && (
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-accent/10 text-accent text-xs font-medium">
              <AlertTriangle className="w-3.5 h-3.5" />
              {session.num_risk_events} risk event(s)
            </span>
          )}
          <span className={cn(
            'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium',
            session.reconciliation_status === 'clean' ? 'bg-primary/10 text-primary' : 'bg-destructive/10 text-destructive'
          )}>
            {session.reconciliation_status === 'clean' ? <CheckCircle className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
            Recon: {session.reconciliation_status}
          </span>
        </div>
      </div>

      {/* Sector Exposure */}
      {sectorExposure.length > 0 && (
        <div className="p-4 rounded-xl border border-border bg-card">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Sector Exposure</h4>
          <div className="space-y-1.5">
            {sectorExposure.map((s) => (
              <div key={s.sector} className="flex items-center gap-2">
                <span className="text-sm w-32 truncate">{s.sector}</span>
                <div className="flex-1 h-2 rounded-full bg-muted overflow-hidden">
                  <div className="h-full bg-primary rounded-full" style={{ width: `${s.percent}%` }} />
                </div>
                <span className="text-xs text-muted-foreground w-12 text-right">{s.percent.toFixed(1)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Per-Trade Log */}
      {trades && trades.length > 0 && (
        <div className="p-4 rounded-xl border border-border bg-card">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">
            Trade Log ({trades.length})
          </h4>
          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground border-b border-border">
                  <th className="text-left py-2 pr-3 font-medium">Time</th>
                  <th className="text-left py-2 pr-3 font-medium">Symbol</th>
                  <th className="text-left py-2 pr-3 font-medium">Side</th>
                  <th className="text-right py-2 pr-3 font-medium">Qty</th>
                  <th className="text-right py-2 pr-3 font-medium">Price</th>
                  <th className="text-right py-2 pr-3 font-medium">P&L</th>
                  <th className="text-right py-2 pr-3 font-medium">Run P&L</th>
                  <th className="text-center py-2 pr-3 font-medium">Outcome</th>
                  <th className="text-right py-2 pr-3 font-medium">Hold</th>
                  <th className="text-right py-2 font-medium">Latency</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i} className="border-b border-border/50 hover:bg-muted/20">
                    <td className="py-2 pr-3 text-muted-foreground">
                      {new Date(t.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
                    </td>
                    <td className="py-2 pr-3 font-medium">{t.symbol}</td>
                    <td className={cn('py-2 pr-3 font-medium', t.side === 'buy' ? 'text-primary' : 'text-destructive')}>
                      {t.side.toUpperCase()}
                    </td>
                    <td className="py-2 pr-3 text-right">{t.quantity}</td>
                    <td className="py-2 pr-3 text-right">${(t.entry_price || 0).toFixed(2)}</td>
                    <td className={cn('py-2 pr-3 text-right font-medium', (t.realized_pnl || 0) > 0 ? 'text-primary' : (t.realized_pnl || 0) < 0 ? 'text-destructive' : '')}>
                      {t.realized_pnl ? fmt(t.realized_pnl) : '—'}
                    </td>
                    <td className="py-2 pr-3 text-right text-muted-foreground">
                      {t.running_daily_pnl != null ? fmt(t.running_daily_pnl) : '—'}
                    </td>
                    <td className="py-2 pr-3 text-center">
                      {t.outcome_label === 'winner' && <span className="text-primary">▲</span>}
                      {t.outcome_label === 'loser' && <span className="text-destructive">▼</span>}
                      {t.outcome_label === 'breakeven' && <span className="text-muted-foreground">●</span>}
                      {t.outcome_label === 'open' && <span className="text-muted-foreground">○</span>}
                    </td>
                    <td className="py-2 pr-3 text-right text-muted-foreground">
                      {t.holding_time_minutes != null ? `${t.holding_time_minutes}m` : '—'}
                    </td>
                    <td className="py-2 text-right text-muted-foreground">
                      {t.execution_latency_ms != null ? `${t.execution_latency_ms}ms` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Audit Events */}
      {auditEvents && auditEvents.length > 0 && (
        <div className="p-4 rounded-xl border border-border bg-card">
          <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">
            Audit Events ({auditEvents.length})
          </h4>
          <div className="space-y-1.5 max-h-48 overflow-y-auto scrollbar-thin">
            {auditEvents.map((a, i) => (
              <div key={i} className="flex items-start gap-2 text-xs">
                <span className="text-muted-foreground w-16 shrink-0">
                  {new Date(a.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
                </span>
                <span className={cn(
                  'px-1.5 py-0.5 rounded font-medium shrink-0',
                  a.severity === 'critical' && 'bg-destructive/10 text-destructive',
                  a.severity === 'error' && 'bg-destructive/10 text-destructive',
                  a.severity === 'warning' && 'bg-accent/10 text-accent',
                  a.severity === 'info' && 'bg-muted text-muted-foreground'
                )}>
                  {a.severity}
                </span>
                <span className="text-muted-foreground shrink-0">{a.event_type}</span>
                <span className="text-foreground truncate">{a.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
