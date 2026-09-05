import { useEffect, useState } from 'react'
import { api } from '../api'
import { time } from '../format'
import { EmptyState } from './Panel'
import type { AiResponse, ScanRun } from '../types'

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

// The benchmark each lane's regime classifier is actually run against
// (scanner/coordinator.py) -- shown alongside the regime label per the
// spec's "LANE • BENCHMARK • REGIME • MULTIPLIERx" format.
const BENCHMARK_BY_LANE: Record<string, string> = { equity: 'SPY', option: 'SPY', crypto: 'BTC/USD' }

function laneState(laneKey: string, marketClosed: boolean, run: ScanRun | undefined): { label: string; className: string } {
  if (marketClosed && !CONTINUOUS_LANES.has(laneKey)) {
    return { label: 'WAITING — MARKET CLOSED', className: 'status-badge status-badge-warning' }
  }
  if (!run) return { label: 'NO DATA YET', className: 'status-badge status-badge-neutral' }
  if (run.status === 'running') return { label: 'ACTIVE — SCANNING', className: 'status-badge status-badge-ok' }
  if (run.status === 'failed') return { label: 'FAILED', className: 'status-badge status-badge-danger' }
  return { label: 'COMPLETED', className: 'status-badge status-badge-ok' }
}

/** `LANE • BENCHMARK • REGIME • MULTIPLIERx` -- an unavailable/fallback
 * classification is visually distinct (`.regime-block-unavailable`), never
 * rendered the same way as a genuine classification, per the truthfulness
 * requirement that "regime unavailable" must never look like a real regime. */
function RegimeLine({ laneKey, label, run }: { laneKey: string; label: string; run: ScanRun }) {
  const benchmark = BENCHMARK_BY_LANE[laneKey] ?? '—'
  const unavailable = !run.regime || run.regime === 'unavailable'
  return (
    <div className={`regime-block${unavailable ? ' regime-block-unavailable' : ''}`}>
      {!run.regime && `${label.toUpperCase()} • ${benchmark} • REGIME NOT YET CLASSIFIED`}
      {run.regime === 'unavailable' &&
        `${label.toUpperCase()} • ${benchmark} • REGIME UNAVAILABLE • FALLBACK ${run.regime_position_multiplier ?? '?'}x`}
      {run.regime && run.regime !== 'unavailable' &&
        `${label.toUpperCase()} • ${benchmark} • ${run.regime.toUpperCase()} • ${run.regime_position_multiplier ?? '?'}x`}
    </div>
  )
}

export function LaneCard({
  laneKey, label, run, marketClosed, rejectedCount,
}: {
  laneKey: string
  label: string
  run: ScanRun | undefined
  marketClosed: boolean
  rejectedCount: number | null
}) {
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
            <dt>Rejected (recent window)</dt>
            <dd>{rejectedCount === null ? 'Unavailable' : rejectedCount}</dd>
            <dt>Orders submitted</dt>
            <dd>{run.orders_submitted}</dd>
            <dt>Market data</dt>
            <dd>
              {run.market_data_tier ?? '—'}
              {feed ? ` (${feed})` : ''}
            </dd>
          </dl>
          <RegimeLine laneKey={laneKey} label={label} run={run} />
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

export const LANES: { key: string; label: string }[] = [
  { key: 'equity', label: 'Equity' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'option', label: 'Options' },
]
