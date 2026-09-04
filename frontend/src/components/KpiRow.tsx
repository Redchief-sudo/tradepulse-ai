import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, pct } from '../format'

function KpiCard({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'positive' | 'negative' }) {
  return (
    <div className="kpi-card">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value num${tone ? ` ${tone}` : ''}`}>{value}</div>
      {sub ? <div className="kpi-sub">{sub}</div> : null}
    </div>
  )
}

export function KpiRow() {
  const { data: account } = usePolling(api.getAccount, 10000)
  const { data: riskExposure } = usePolling(api.getRiskExposure, 15000)
  const { data: pnl } = usePolling(api.getPnl, 15000)

  const dailyPnlPct = riskExposure ? Number(riskExposure.daily_pnl_pct) : null
  const unrealizedTotal = pnl ? Number(pnl.unrealized_total) : null
  // Display-only subtotal of already-authoritative per-record realized P&L
  // values -- the same arithmetic PnlPanel's table already lets a reader do
  // by hand, not a new financial computation.
  const realizedTotal = pnl ? pnl.realized.reduce((sum, r) => sum + Number(r.realized), 0) : null

  return (
    <div className="kpi-row">
      <KpiCard label="Account Equity" value={account ? money(account.equity) : '—'} />
      <KpiCard label="Cash" value={account ? money(account.cash) : '—'} />
      <KpiCard label="Buying Power" value={account ? money(account.buying_power) : '—'} />
      <KpiCard
        label="Daily P&L"
        value={dailyPnlPct !== null ? pct(dailyPnlPct) : '—'}
        tone={dailyPnlPct !== null ? (dailyPnlPct >= 0 ? 'positive' : 'negative') : undefined}
      />
      <KpiCard
        label="Unrealized P&L"
        value={unrealizedTotal !== null ? money(unrealizedTotal) : '—'}
        tone={unrealizedTotal !== null ? (unrealizedTotal >= 0 ? 'positive' : 'negative') : undefined}
      />
      <KpiCard
        label="Realized P&L"
        value={realizedTotal !== null ? money(realizedTotal) : '—'}
        sub="recent window"
        tone={realizedTotal !== null ? (realizedTotal >= 0 ? 'positive' : 'negative') : undefined}
      />
      <KpiCard label="Open Positions" value={riskExposure ? String(riskExposure.open_positions) : '—'} />
    </div>
  )
}
