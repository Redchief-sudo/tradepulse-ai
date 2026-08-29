import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, time } from '../format'
import { Panel } from './Panel'

export function PnlPanel() {
  const { data, error, loading } = usePolling(api.getPnl, 15000)
  return (
    <Panel title="PnL" error={error} loading={loading}>
      {data && (
        <>
          <dl className="kv">
            <dt>Unrealized total (live positions)</dt>
            <dd className={Number(data.unrealized_total) >= 0 ? 'positive' : 'negative'}>{money(data.unrealized_total)}</dd>
          </dl>
          <h3>Recent realized</h3>
          {data.realized.length === 0 && <p className="muted">No realized P&amp;L records yet.</p>}
          {data.realized.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Realized</th>
                  <th>Unrealized (at record time)</th>
                  <th>As of</th>
                </tr>
              </thead>
              <tbody>
                {data.realized.map((r) => (
                  <tr key={r.record_id}>
                    <td>{r.asset.symbol}</td>
                    <td className={Number(r.realized) >= 0 ? 'positive' : 'negative'}>{money(r.realized)}</td>
                    <td>{money(r.unrealized)}</td>
                    <td>{time(r.as_of)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Panel>
  )
}
