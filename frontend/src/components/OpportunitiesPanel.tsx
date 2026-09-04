import { Fragment, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { Opportunity } from '../types'

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
          <td colSpan={8}>
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

export function OpportunitiesPanel() {
  const { data, error, loading } = usePolling(() => api.getOpportunities(30), 30000)
  return (
    <Panel title="Recent Opportunities" error={error} loading={loading}>
      <p className="muted">
        Approved candidates only -- rejected candidates are logged but not yet persisted (see the plan's non-goals).
      </p>
      {data && data.length === 0 && <EmptyState>No opportunities yet.</EmptyState>}
      {data && data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
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
            {data.map((opp) => (
              <OpportunityRow key={opp.opportunity_id} opp={opp} />
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
