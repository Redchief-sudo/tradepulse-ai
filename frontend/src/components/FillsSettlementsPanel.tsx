import { money, time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { Fill, SettlementEvent } from '../types'

function settlementStatusTone(status: string): string {
  if (status === 'completed') return 'status-badge-ok'
  if (status === 'terminal_failed' || status === 'integrity_blocked') return 'status-badge-danger'
  if (status === 'retryable_failed') return 'status-badge-warning'
  return 'status-badge-neutral'
}

function StatusBadge({ value, tone }: { value: string; tone: string }) {
  return <span className={`status-badge ${tone}`}>{value.replace(/_/g, ' ')}</span>
}

export function FillsSettlementsPanel({
  fills, settlements,
}: {
  fills: { data: Fill[] | null; error: string | null; loading: boolean }
  settlements: { data: SettlementEvent[] | null; error: string | null; loading: boolean }
}) {
  return (
    <Panel title="Fills &amp; Settlement" error={fills.error ?? settlements.error} loading={fills.loading || settlements.loading}>
      <h3>Recent fills</h3>
      {fills.data && fills.data.length === 0 && <EmptyState>No fills yet.</EmptyState>}
      {fills.data && fills.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Price</th>
              <th className="num">Fees</th>
              <th>Filled</th>
            </tr>
          </thead>
          <tbody>
            {fills.data.map((fill) => (
              <tr key={fill.fill_id}>
                <td>{fill.asset.symbol}</td>
                <td>{fill.side}</td>
                <td className="num">{fill.quantity}</td>
                <td className="num">{money(fill.price)}</td>
                <td className="num">{money(fill.fees)}</td>
                <td>{time(fill.filled_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Recent settlements</h3>
      {settlements.data && settlements.data.length === 0 && <EmptyState>No settlements yet.</EmptyState>}
      {settlements.data && settlements.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th className="num">Realized P&amp;L</th>
              <th>Occurred</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {settlements.data.map((s) => (
              <tr key={s.settlement_event_id} className={s.status === 'integrity_blocked' || s.status === 'terminal_failed' ? 'row-danger' : ''}>
                <td>{s.asset.symbol}</td>
                <td>{s.side}</td>
                <td>
                  <StatusBadge value={s.status} tone={settlementStatusTone(s.status)} />
                </td>
                <td className={`num ${s.realized_pnl !== null ? (Number(s.realized_pnl) >= 0 ? 'positive' : 'negative') : ''}`}>
                  {money(s.realized_pnl)}
                </td>
                <td>{time(s.occurred_at)}</td>
                <td>{s.error_code ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
