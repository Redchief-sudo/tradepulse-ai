import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, pct } from '../format'
import { Panel, EmptyState } from './Panel'
import { OccContractLabel } from './OccContractLabel'

export function PositionsPanel() {
  const { data, error, loading } = usePolling(api.getPositions, 10000)
  return (
    <Panel title="Open Positions" error={error} loading={loading}>
      {data && data.length === 0 && <EmptyState>No open positions.</EmptyState>}
      {data && data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Class</th>
              <th className="num">Qty</th>
              <th className="num">Avg entry</th>
              <th className="num">Current</th>
              <th className="num">Market value</th>
              <th className="num">Unrealized P&amp;L</th>
              <th className="num">Unrealized %</th>
              <th className="num">Stop</th>
              <th className="num">Target</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row) => {
              const qty = Number(row.position.qty)
              const avgEntry = Number(row.position.avg_entry_price)
              const current = Number(row.position.current_price)
              const marketValue = qty * current
              const costBasis = qty * avgEntry
              const unrealizedPct = costBasis !== 0 ? (Number(row.position.unrealized_pl) / costBasis) * 100 : null
              const pnlTone = Number(row.position.unrealized_pl) >= 0 ? 'positive' : 'negative'
              return (
                <tr key={row.position.symbol} data-asset-class={row.position.asset_class} className="asset-class-row">
                  <td>
                    {row.position.asset_class === 'option' ? <OccContractLabel symbol={row.position.symbol} /> : row.position.symbol}
                  </td>
                  <td>
                    <span className={`status-badge status-badge-${row.position.asset_class === 'crypto' ? 'hold' : row.position.asset_class === 'option' ? 'warning' : 'ok'}`}>
                      {row.position.asset_class}
                    </span>
                  </td>
                  <td className="num">{row.position.qty}</td>
                  <td className="num">{money(row.position.avg_entry_price)}</td>
                  <td className="num">{money(row.position.current_price)}</td>
                  <td className="num">{money(marketValue)}</td>
                  <td className={`num ${pnlTone}`}>{money(row.position.unrealized_pl)}</td>
                  <td className={`num ${unrealizedPct !== null ? (unrealizedPct >= 0 ? 'positive' : 'negative') : ''}`}>
                    {unrealizedPct !== null ? pct(unrealizedPct) : '—'}
                  </td>
                  <td className="num">{money(row.stop_loss)}</td>
                  <td className="num">{money(row.target_price)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
