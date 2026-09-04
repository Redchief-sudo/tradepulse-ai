import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { AiResponse, ScanRun } from '../types'

const LANES: { key: string; label: string }[] = [
  { key: 'equity', label: 'Equity' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'option', label: 'Options' },
]

/** AIResponse rows are immutable once persisted -- a one-shot fetch keyed by
 * request_id, not polling, so this doesn't re-fetch on every panel refresh.
 * State is only ever set from the async callbacks below (never
 * synchronously in the effect body); a stale in-flight result for a
 * since-changed requestId is filtered out at render time instead. */
function useAiCandidates(requestId: string | null): AiResponse | null {
  const [state, setState] = useState<{ requestId: string | null; response: AiResponse | null }>({
    requestId: null,
    response: null,
  })
  useEffect(() => {
    if (!requestId) return
    let cancelled = false
    api
      .getAiResponse(requestId)
      .then((r) => {
        if (!cancelled) setState({ requestId, response: r })
      })
      .catch(() => {
        if (!cancelled) setState({ requestId, response: null })
      })
    return () => {
      cancelled = true
    }
  }, [requestId])
  return state.requestId === requestId ? state.response : null
}

// Verified against risk/session.py::execution_session_decision and
// models/market.py::is_continuous_market -- MARKET_CLOSED exempts only
// continuous markets, which today means crypto exclusively.
const CONTINUOUS_LANES = new Set(['crypto'])

function laneState(laneKey: string, marketClosed: boolean, run: ScanRun | undefined): { label: string; className: string } {
  if (marketClosed && !CONTINUOUS_LANES.has(laneKey)) {
    return { label: 'WAITING — MARKET CLOSED', className: 'status-badge status-badge-warning' }
  }
  if (!run) return { label: 'NO DATA YET', className: 'status-badge status-badge-neutral' }
  if (run.status === 'running') return { label: 'ACTIVE — SCANNING', className: 'status-badge status-badge-ok' }
  if (run.status === 'failed') return { label: 'FAILED', className: 'status-badge status-badge-danger' }
  return { label: 'COMPLETED', className: 'status-badge status-badge-ok' }
}

function LaneCard({ laneKey, label, run, marketClosed }: { laneKey: string; label: string; run: ScanRun | undefined; marketClosed: boolean }) {
  const aiResponse = useAiCandidates(run?.ai_response_request_id ?? null)
  const feed = run?.asset_class === 'option' ? run.option_feed : run?.asset_class === 'equity' ? run.equity_feed : null
  const state = laneState(laneKey, marketClosed, run)

  return (
    <div className="lane-card">
      <div className="lane-header">
        <h3>{label}</h3>
        <span className={state.className}>{state.label}</span>
      </div>
      {!run && <EmptyState>No scan cycle recorded yet.</EmptyState>}
      {run && (
        <>
          <dl className="kv">
            <dt>Status</dt>
            <dd>{run.status.toUpperCase()}</dd>
            <dt>Started</dt>
            <dd>{time(run.started_at)}</dd>
            <dt>Completed</dt>
            <dd>{time(run.completed_at)}</dd>
            <dt>Configured universe</dt>
            <dd>{run.universe_size}</dd>
            <dt>AI candidates</dt>
            <dd>{run.candidates_discovered}</dd>
            <dt>Approved</dt>
            <dd>{run.candidates_approved}</dd>
            <dt>Orders submitted</dt>
            <dd>{run.orders_submitted}</dd>
            <dt>Market data</dt>
            <dd>
              {run.market_data_tier ?? '—'}
              {feed ? ` (${feed})` : ''}
            </dd>
          </dl>
          <div className={`regime-block${!run.regime || run.regime === 'unavailable' ? ' regime-block-unavailable' : ''}`}>
            {!run.regime && 'Regime: —'}
            {run.regime === 'unavailable' && `Regime: unavailable (${run.regime_reason ?? 'unknown'})`}
            {run.regime && run.regime !== 'unavailable' && `Regime: ${run.regime} (${run.regime_position_multiplier ?? '?'}x)`}
          </div>
          {run.error && <div className="panel-error">{run.error}</div>}
          {aiResponse && aiResponse.result.candidates.length > 0 && (
            <>
              <h3>AI candidates this cycle</h3>
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Recommendation</th>
                    <th>Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {aiResponse.result.candidates.map((c) => (
                    <tr key={c.symbol}>
                      <td>{c.symbol}</td>
                      <td>{c.recommendation}</td>
                      <td>{c.confidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  )
}

export function ScannerActivityPanel() {
  const { data, error, loading } = usePolling(() => api.getScanRuns(50), 20000)
  const { data: session } = usePolling(api.getSession, 5000)

  const latestByLane: Record<string, ScanRun> = {}
  if (data) {
    for (const run of data) {
      if (!(run.asset_class in latestByLane)) latestByLane[run.asset_class] = run
    }
  }

  const marketClosed = session?.state === 'market_closed'

  return (
    <Panel title="Scanner Activity" error={error} loading={loading}>
      <p className="muted">
        "AI candidates" are what the AI returned this cycle -- not a claim they all reached market-data evaluation.
        "Approved" candidates cleared the full deterministic/risk gate chain (see Recent Opportunities for their
        detail). Rejection reasons are log-only in this pass, not shown here.
      </p>
      {LANES.map(({ key, label }) => (
        <LaneCard key={key} laneKey={key} label={label} run={latestByLane[key]} marketClosed={marketClosed} />
      ))}
    </Panel>
  )
}
