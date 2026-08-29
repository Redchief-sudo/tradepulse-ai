import { api } from '../api'
import { usePolling } from '../usePolling'
import { money } from '../format'
import { Panel } from './Panel'

export function PositionsPanel() {
  const { data, error, loading } = usePolling(api.getPositions, 10000)
  return (
    <Panel title="Open Positions" error={error} loading={loading}>
      {data && data.length === 0 && <p className="muted">No open positions.</p>}
      {data && data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Class</th>
              <th>Qty</th>
              <th>Avg entry</th>
              <th>Current</th>
              <th>Unrealized P&amp;L</th>
              <th>Stop</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row) => (
              <tr key={row.position.symbol}>
                <td>{row.position.symbol}</td>
                <td>{row.position.asset_class}</td>
                <td>{row.position.qty}</td>
                <td>{money(row.position.avg_entry_price)}</td>
                <td>{money(row.position.current_price)}</td>
                <td className={Number(row.position.unrealized_pl) >= 0 ? 'positive' : 'negative'}>{money(row.position.unrealized_pl)}</td>
                <td>{money(row.stop_loss)}</td>
                <td>{money(row.target_price)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
