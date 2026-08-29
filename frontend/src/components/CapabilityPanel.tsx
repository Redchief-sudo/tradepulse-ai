import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePolling } from '../usePolling'
import { Panel } from './Panel'

/** Reads what the last real scan cycle per lane actually used (from
 * ScanRun.market_data_tier/equity_feed/option_feed) -- NOT a live probe.
 * "Probe now" is a separate, explicit action for "what would AUTO resolve
 * to right now," never conflated with "what's actually in use." */
export function CapabilityPanel() {
  const { data, error, loading } = usePolling(api.getMarketDataCapability, 30000)
  const [probeResult, setProbeResult] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState<string | null>(null)

  async function probe() {
    setProbing(true)
    setProbeError(null)
    try {
      const result = await api.probeMarketDataCapability()
      setProbeResult(`${result.tier_label} (equity=${result.equity_feed}, option=${result.option_feed})`)
    } catch (err) {
      setProbeError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setProbing(false)
    }
  }

  const lanes = data ? Object.entries(data) : []

  return (
    <Panel title="Market Data Capability" error={error} loading={loading}>
      {lanes.length === 0 && <p className="muted">No scan cycle has recorded a resolved capability yet.</p>}
      <table>
        <thead>
          <tr>
            <th>Lane</th>
            <th>Tier</th>
            <th>Equity feed</th>
            <th>Option feed</th>
          </tr>
        </thead>
        <tbody>
          {lanes.map(([lane, run]) => (
            <tr key={lane}>
              <td>{lane}</td>
              <td>{run.market_data_tier ?? '—'}</td>
              <td>{run.equity_feed ?? '—'}</td>
              <td>{run.option_feed ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="button-row">
        <button disabled={probing} onClick={probe}>
          Probe live now
        </button>
      </div>
      {probeResult && <p className="muted">Live probe result: {probeResult}</p>}
      {probeError && <div className="panel-error">{probeError}</div>}
    </Panel>
  )
}
