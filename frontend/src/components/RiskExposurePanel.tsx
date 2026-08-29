import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, pct } from '../format'
import { Panel } from './Panel'

export function RiskExposurePanel() {
  const { data, error, loading } = usePolling(api.getRiskExposure, 15000)
  return (
    <Panel title="Risk Exposure" error={error} loading={loading}>
      {data && (
        <>
          <dl className="kv">
            <dt>Total equity</dt>
            <dd>{money(data.total_equity)}</dd>
            <dt>Holdings value</dt>
            <dd>{money(data.holdings_value)}</dd>
            <dt>Cash</dt>
            <dd>{money(data.cash_balance)}</dd>
            <dt>Open positions</dt>
            <dd>{data.open_positions}</dd>
            <dt>Outstanding orders</dt>
            <dd>{data.outstanding_orders}</dd>
            <dt>Trades today</dt>
            <dd>{data.trades_today}</dd>
            <dt>Daily P&amp;L</dt>
            <dd className={Number(data.daily_pnl_pct) >= 0 ? 'positive' : 'negative'}>{pct(data.daily_pnl_pct)}</dd>
            <dt>Equity source</dt>
            <dd>{data.source}</dd>
          </dl>
          {Object.keys(data.sector_exposure).length > 0 && (
            <>
              <h3>Sector exposure</h3>
              <table>
                <thead>
                  <tr>
                    <th>Sector</th>
                    <th>Notional</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.sector_exposure).map(([sector, value]) => (
                    <tr key={sector}>
                      <td>{sector}</td>
                      <td>{money(value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </Panel>
  )
}
