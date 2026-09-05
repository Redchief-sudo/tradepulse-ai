import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePolling } from '../usePolling'
import { relativeAgo } from '../format'
import { Panel, EmptyState } from './Panel'

/** Reads what the last real scan cycle per lane actually used (from
 * ScanRun.market_data_tier/equity_feed/option_feed) -- NOT a live probe.
 * "Probe now" is a separate, explicit action for "what would AUTO resolve
 * to right now," never conflated with "what's actually in use." */
export function CapabilityPanel() {
  const { data, error, loading } = usePolling(api.getMarketDataCapability, 30000)
  const [probeResult, setProbeResult] = useState<string | null>(null)
  const [probedAtIso, setProbedAtIso] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState<string | null>(null)

  async function probe() {
    setProbing(true)
    setProbeError(null)
    try {
      const result = await api.probeMarketDataCapability()
      setProbeResult(`${result.tier_label} (equity=${result.equity_feed}, option=${result.option_feed})`)
      setProbedAtIso(new Date().toISOString())
    } catch (err) {
      setProbeError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setProbing(false)
    }
  }

  const lanes = data ? Object.entries(data) : []

  return (
    <Panel title="Market Data Capability" error={error} loading={loading}>
      {lanes.length === 0 && <EmptyState>No scan cycle has recorded a resolved capability yet.</EmptyState>}
      {lanes.length > 0 && (
        <div className="capability-cards">
          {lanes.map(([lane, run]) => (
            <div key={lane} className="capability-card">
              <div className="capability-card-lane">{lane}</div>
              <dl className="kv">
                <dt>Tier</dt>
                <dd>{run.market_data_tier ?? 'Unavailable'}</dd>
                <dt>Equity feed</dt>
                <dd>{run.equity_feed ?? '—'}</dd>
                <dt>Option feed</dt>
                <dd>{run.option_feed ?? '—'}</dd>
                <dt>As of</dt>
                <dd>{run.completed_at ? relativeAgo(run.completed_at) : '—'}</dd>
              </dl>
            </div>
          ))}
        </div>
      )}
      <div className="button-row">
        <button disabled={probing} onClick={probe}>
          Probe live now
        </button>
      </div>
      {probeResult && (
        <p className="muted">
          Live probe result: {probeResult}
          {probedAtIso ? ` (${relativeAgo(probedAtIso)})` : ''}
        </p>
      )}
      {probeError && <div className="panel-error">{probeError}</div>}
    </Panel>
  )
}
