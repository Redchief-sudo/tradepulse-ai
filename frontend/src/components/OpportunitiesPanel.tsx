import { Fragment, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { Opportunity, RejectedCandidate } from '../types'

function signalTone(signal: string | null | undefined): string {
  const s = (signal ?? '').toUpperCase()
  if (s === 'BUY' || s === 'STRONG_BUY') return 'status-badge-buy'
  if (s === 'SELL' || s === 'STRONG_SELL') return 'status-badge-sell'
  if (s === 'HOLD') return 'status-badge-hold'
  return 'status-badge-neutral'
}

function SignalBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="status-badge status-badge-neutral">—</span>
  return <span className={`status-badge ${signalTone(value)}`}>{value.replace(/_/g, ' ')}</span>
}

const DETAIL_KEYS: [string, string][] = [
  ['factor_breakdown', 'Factor breakdown'],
  ['sector', 'Sector'],
  ['max_correlation', 'Max correlation'],
  ['weighted_composite', 'Weighted composite'],
  ['technical_score', 'Technical score'],
  ['momentum_score', 'Momentum score'],
  ['risk_score', 'Risk score'],
]

function OpportunityRow({ opp }: { opp: Opportunity }) {
  const [expanded, setExpanded] = useState(false)
  const detailFields = DETAIL_KEYS.filter(([key]) => opp.metadata[key] !== undefined && opp.metadata[key] !== null)

  return (
    <>
      <tr>
        <td>{opp.asset.symbol}</td>
        <td>{opp.asset.asset_class}</td>
        <td>{opp.source}</td>
        <td>{opp.confidence ?? '—'}</td>
        <td>
          <SignalBadge value={opp.metadata.ai_recommendation} />
        </td>
        <td>
          <SignalBadge value={opp.metadata.deterministic_signal} />
        </td>
        <td>
          {opp.metadata.market_data_feed ?? '—'} ({opp.metadata.market_data_authority ?? '—'})
        </td>
        <td>{time(opp.created_at)}</td>
        <td>
          {detailFields.length > 0 && (
            <button onClick={() => setExpanded((v) => !v)}>{expanded ? 'Hide' : 'Detail'}</button>
          )}
        </td>
      </tr>
      {expanded && detailFields.length > 0 && (
        <tr>
          <td colSpan={9}>
            <dl className="kv">
              {detailFields.map(([key, label]) => (
                <Fragment key={key}>
                  <dt>{label}</dt>
                  <dd>{String(opp.metadata[key])}</dd>
                </Fragment>
              ))}
            </dl>
          </td>
        </tr>
      )}
    </>
  )
}

function RejectedRow({ row }: { row: RejectedCandidate }) {
  return (
    <tr className="row-warning">
      <td>{row.symbol}</td>
      <td>{row.asset_class}</td>
      <td colSpan={4}>{row.reason}</td>
      <td>{time(row.occurred_at)}</td>
      <td></td>
    </tr>
  )
}

type Filter = 'all' | 'equity' | 'crypto' | 'option' | 'approved' | 'rejected'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'equity', label: 'Equity' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'option', label: 'Options' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
]

export function OpportunitiesPanel({
  data, error, loading,
}: {
  data: Opportunity[] | null
  error: string | null
  loading: boolean
}) {
  const [filter, setFilter] = useState<Filter>('all')
  const rejected = usePolling(() => api.getRejectedCandidates(50), 30000)

  const showRejected = filter === 'rejected'
  const filteredOpportunities = (data ?? []).filter((o) => {
    if (filter === 'all' || filter === 'approved') return true
    if (filter === 'rejected') return false
    return o.asset.asset_class === filter
  })
  const filteredRejected = rejected.data ?? [] // a single flat "Rejected" tab, matching the spec's mutually-exclusive filter list

  const activeError = showRejected ? rejected.error : error
  const activeLoading = showRejected ? rejected.loading : loading

  return (
    <Panel title="Recent Opportunities" error={activeError} loading={activeLoading}>
      <p className="muted">
        "Approved" opportunities cleared the full deterministic/risk gate chain. "Rejected" candidates come from a
        separate, persisted rejection log (reason codes, not the same shape as an approved opportunity).
      </p>
      <div className="button-row">
        {FILTERS.map((f) => (
          <button key={f.key} className={filter === f.key ? 'filter-active' : ''} onClick={() => setFilter(f.key)}>
            {f.label}
          </button>
        ))}
      </div>
      {!showRejected && filteredOpportunities.length === 0 && <EmptyState>No opportunities yet.</EmptyState>}
      {!showRejected && filteredOpportunities.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Class</th>
              <th>Source</th>
              <th>Confidence</th>
              <th>AI recommendation</th>
              <th>Deterministic signal</th>
              <th>Feed</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filteredOpportunities.map((opp) => (
              <OpportunityRow key={opp.opportunity_id} opp={opp} />
            ))}
          </tbody>
        </table>
      )}
      {showRejected && filteredRejected.length === 0 && <EmptyState>No rejected candidates in the recent window.</EmptyState>}
      {showRejected && filteredRejected.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Class</th>
              <th colSpan={4}>Reason</th>
              <th>Occurred</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filteredRejected.map((row) => (
              <RejectedRow key={row.rejection_id} row={row} />
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
