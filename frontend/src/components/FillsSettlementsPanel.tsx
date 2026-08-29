import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, time } from '../format'
import { Panel } from './Panel'

export function FillsSettlementsPanel() {
  const fills = usePolling(() => api.getFills(20), 30000)
  const settlements = usePolling(() => api.getSettlements(20), 30000)

  return (
    <Panel title="Fills &amp; Settlement" error={fills.error ?? settlements.error} loading={fills.loading || settlements.loading}>
      <h3>Recent fills</h3>
      {fills.data && fills.data.length === 0 && <p className="muted">No fills yet.</p>}
      {fills.data && fills.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Fees</th>
              <th>Filled</th>
            </tr>
          </thead>
          <tbody>
            {fills.data.map((fill) => (
              <tr key={fill.fill_id}>
                <td>{fill.asset.symbol}</td>
                <td>{fill.side}</td>
                <td>{fill.quantity}</td>
                <td>{money(fill.price)}</td>
                <td>{money(fill.fees)}</td>
                <td>{time(fill.filled_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Recent settlements</h3>
      {settlements.data && settlements.data.length === 0 && <p className="muted">No settlements yet.</p>}
      {settlements.data && settlements.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th>Realized P&amp;L</th>
              <th>Occurred</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {settlements.data.map((s) => (
              <tr key={s.settlement_event_id} className={s.status === 'integrity_blocked' || s.status === 'terminal_failed' ? 'row-danger' : ''}>
                <td>{s.asset.symbol}</td>
                <td>{s.side}</td>
                <td>{s.status}</td>
                <td>{money(s.realized_pnl)}</td>
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
