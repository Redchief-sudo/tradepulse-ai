import { api } from '../api'
import { usePolling } from '../usePolling'
import { money, pct } from '../format'
import { Panel } from './Panel'

function utilTone(utilizationPct: number): 'ok' | 'warn' | 'critical' {
  if (utilizationPct > 90) return 'critical'
  if (utilizationPct >= 70) return 'warn'
  return 'ok'
}

function UtilBar({ label, utilizationPct }: { label: string; utilizationPct: number }) {
  const tone = utilTone(utilizationPct)
  return (
    <div className="util-bar-row">
      <span className="util-bar-label">{label}</span>
      <span className="util-bar-track">
        <span
          className={`util-bar-fill util-bar-fill-${tone}`}
          style={{ width: `${Math.min(100, Math.max(0, utilizationPct))}%` }}
        />
      </span>
      <span className="util-bar-pct">{utilizationPct.toFixed(0)}%</span>
    </div>
  )
}

function amountBar(key: string, label: string, actual: number, limitAmount: number) {
  return limitAmount > 0 ? <UtilBar key={key} label={label} utilizationPct={(actual / limitAmount) * 100} /> : null
}

export function RiskExposurePanel() {
  const { data, error, loading } = usePolling(api.getRiskExposure, 15000)
  const { data: limits } = usePolling(api.getRiskLimits, 30000)
  const { data: positions } = usePolling(api.getPositions, 10000)

  const totalEquity = data ? Number(data.total_equity) : 0
  const portfolioLimitAmount = limits && Number(limits.max_total_exposure_pct) > 0 ? (Number(limits.max_total_exposure_pct) / 100) * totalEquity : 0
  const sectorLimitAmount = limits && Number(limits.max_sector_pct) > 0 ? (Number(limits.max_sector_pct) / 100) * totalEquity : 0
  const positionLimitAmount = limits && Number(limits.max_position_pct) > 0 ? (Number(limits.max_position_pct) / 100) * totalEquity : 0

  const dailyPnlPct = data ? Number(data.daily_pnl_pct) : 0
  const dailyLossLimitPct = limits ? Number(limits.max_daily_loss_pct) : 0
  const dailyLossUtilizationPct = dailyLossLimitPct > 0 && dailyPnlPct < 0 ? (Math.abs(dailyPnlPct) / dailyLossLimitPct) * 100 : 0

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

          {(portfolioLimitAmount > 0 || dailyLossLimitPct > 0) && (
            <>
              <h3>Utilization vs. active risk profile{limits ? ` (${limits.profile_id})` : ''}</h3>
              {amountBar('portfolio', 'Portfolio exposure', Number(data.holdings_value), portfolioLimitAmount)}
              {dailyLossLimitPct > 0 && <UtilBar label="Daily loss budget" utilizationPct={dailyLossUtilizationPct} />}
            </>
          )}

          {positions && positions.length > 0 && positionLimitAmount > 0 && (
            <>
              <h3>Position-cap utilization</h3>
              {positions.map((p) => amountBar(p.position.symbol, p.position.symbol, Number(p.position.qty) * Number(p.position.current_price), positionLimitAmount))}
            </>
          )}

          {Object.keys(data.sector_exposure).length > 0 && (
            <>
              <h3>Sector exposure</h3>
              {sectorLimitAmount > 0 &&
                Object.entries(data.sector_exposure).map(([sector, value]) => amountBar(sector, sector, Number(value), sectorLimitAmount))}
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
