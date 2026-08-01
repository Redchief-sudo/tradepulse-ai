import React from 'react';
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet';
import { ShieldCheck, ShieldAlert, Wallet, Activity } from 'lucide-react';

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

const STATE_FLOW = ['proposed', 'risk_approved', 'submitted', 'accepted', 'filled', 'settled'];

function safeParse(s) {
  if (!s) return null;
  try { return JSON.parse(s); } catch { return null; }
}

function fmtMoney(n) {
  if (n == null || isNaN(n)) return '—';
  return `$${Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function IntentDetail({ intent, open, onOpenChange }) {
  if (!intent) return null;
  const risk = safeParse(intent.risk_snapshot);
  const portfolio = safeParse(intent.portfolio_snapshot);
  const currentStateIdx = STATE_FLOW.indexOf(intent.status);
  const isRejected = ['rejected', 'canceled', 'expired', 'failed'].includes(intent.status);
  const riskPassed = intent.status === 'risk_approved' || currentStateIdx > 0;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <span>{intent.symbol}</span>
            <span className={intent.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{intent.side}</span>
            <span className={`text-xs px-2 py-0.5 rounded border ${STATUS_STYLE[intent.status] || ''}`}>{intent.status}</span>
          </SheetTitle>
          <SheetDescription className="font-mono text-xs">{intent.trade_intent_id}</SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-5">
          {/* State machine flow */}
          <div>
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">State Machine</h4>
            <div className="flex items-center gap-1 flex-wrap">
              {STATE_FLOW.map((s, idx) => (
                <div key={s} className="flex items-center gap-1">
                  <span className={`text-xs px-2 py-1 rounded border ${idx <= currentStateIdx && !isRejected ? STATUS_STYLE[s] : 'text-muted-foreground bg-muted/20 border-border'}`}>{s}</span>
                  {idx < STATE_FLOW.length - 1 && <span className="text-muted-foreground text-xs">→</span>}
                </div>
              ))}
            </div>
            {isRejected && <p className="text-xs text-red-400 mt-2">Terminated at: {intent.status}</p>}
          </div>

          {/* Risk evaluation */}
          <div className="rounded-xl border border-border bg-muted/20 p-4">
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-1.5">
              {riskPassed && !isRejected ? <ShieldCheck className="w-3.5 h-3.5 text-primary" /> : <ShieldAlert className="w-3.5 h-3.5 text-red-400" />}
              Risk Evaluation
            </h4>
            {risk ? (
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">Approved Qty</span><span className="font-mono">{risk.approvedQuantity ?? intent.requested_quantity}</span></div>
                {risk.reasons?.length > 0 && (
                  <div>
                    <span className="text-muted-foreground text-xs">Reasons:</span>
                    <ul className="mt-1 space-y-1">
                      {risk.reasons.map((r, i) => <li key={i} className="text-xs text-amber-400 flex gap-1.5"><span>•</span><span>{r}</span></li>)}
                    </ul>
                  </div>
                )}
                {risk.limits && (
                  <div className="pt-2 border-t border-border/50">
                    <span className="text-muted-foreground text-xs">Limits applied:</span>
                    <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                      {Object.entries(risk.limits).map(([k, v]) => (
                        <div key={k} className="flex justify-between"><span className="text-muted-foreground">{k}</span><span className="font-mono">{String(v)}</span></div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : <p className="text-xs text-muted-foreground">No risk snapshot recorded.</p>}
          </div>

          {/* Portfolio snapshot */}
          {portfolio && (
            <div className="rounded-xl border border-border bg-muted/20 p-4">
              <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-1.5"><Wallet className="w-3.5 h-3.5 text-accent" /> Portfolio at Signal Time</h4>
              <div className="space-y-1.5 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">Total Equity</span><span className="font-mono">{fmtMoney(portfolio.totalEquity)}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Open Positions</span><span className="font-mono">{portfolio.openPositions}</span></div>
                {portfolio.sectorMap && Object.keys(portfolio.sectorMap).length > 0 && (
                  <div className="pt-2 border-t border-border/50">
                    <span className="text-muted-foreground text-xs">Sector exposure:</span>
                    <div className="mt-1 space-y-1">
                      {Object.entries(portfolio.sectorMap).map(([sector, pct]) => (
                        <div key={sector} className="flex justify-between text-xs"><span className="text-muted-foreground">{sector}</span><span className="font-mono">{typeof pct === 'number' ? `${pct.toFixed(1)}%` : String(pct)}</span></div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Execution details */}
          <div className="rounded-xl border border-border bg-muted/20 p-4">
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3 flex items-center gap-1.5"><Activity className="w-3.5 h-3.5 text-chart-3" /> Execution</h4>
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <div><span className="text-muted-foreground text-xs block">Mode</span><span>{intent.execution_mode}</span></div>
              <div><span className="text-muted-foreground text-xs block">Broker</span><span>{intent.broker || '—'}</span></div>
              <div><span className="text-muted-foreground text-xs block">Order Type</span><span>{intent.order_type}</span></div>
              <div><span className="text-muted-foreground text-xs block">TIF</span><span>{intent.time_in_force}</span></div>
              <div><span className="text-muted-foreground text-xs block">Requested Qty</span><span className="font-mono">{intent.requested_quantity}</span></div>
              <div><span className="text-muted-foreground text-xs block">Filled Qty</span><span className="font-mono">{intent.filled_quantity ?? '—'}</span></div>
              <div><span className="text-muted-foreground text-xs block">Fill Price</span><span className="font-mono">{intent.filled_avg_price ? fmtMoney(intent.filled_avg_price) : '—'}</span></div>
              <div><span className="text-muted-foreground text-xs block">Commission</span><span className="font-mono">{intent.commission ? fmtMoney(intent.commission) : '—'}</span></div>
              <div><span className="text-muted-foreground text-xs block">Slippage</span><span className="font-mono">{intent.slippage ? fmtMoney(intent.slippage) : '—'}</span></div>
              <div><span className="text-muted-foreground text-xs block">Realized P&L</span><span className={`font-mono ${intent.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{intent.realized_pnl != null ? fmtMoney(intent.realized_pnl) : '—'}</span></div>
            </div>
          </div>

          {/* Rejection reason */}
          {intent.rejection_reason && (
            <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-4">
              <h4 className="text-xs font-semibold text-red-400 uppercase tracking-wider mb-2">Rejection Reason</h4>
              <p className="text-sm text-red-300">{intent.rejection_reason}</p>
            </div>
          )}

          {/* Timestamps */}
          <div className="text-xs text-muted-foreground space-y-1 pt-2 border-t border-border">
            <div className="flex justify-between"><span>Signal</span><span>{intent.signal_timestamp ? new Date(intent.signal_timestamp).toLocaleString() : '—'}</span></div>
            <div className="flex justify-between"><span>Decision</span><span>{intent.decision_timestamp ? new Date(intent.decision_timestamp).toLocaleString() : '—'}</span></div>
            <div className="flex justify-between"><span>Created</span><span>{intent.created_date ? new Date(intent.created_date).toLocaleString() : '—'}</span></div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}