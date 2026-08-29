import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel } from './Panel'

export function OpportunitiesPanel() {
  const { data, error, loading } = usePolling(() => api.getOpportunities(30), 30000)
  return (
    <Panel title="Recent Opportunities" error={error} loading={loading}>
      <p className="muted">
        Approved candidates only -- rejected candidates are logged but not yet persisted (see the plan's non-goals).
      </p>
      {data && data.length === 0 && <p className="muted">No opportunities yet.</p>}
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
            </tr>
          </thead>
          <tbody>
            {data.map((opp) => (
              <tr key={opp.opportunity_id}>
                <td>{opp.asset.symbol}</td>
                <td>{opp.source}</td>
                <td>{opp.confidence ?? '—'}</td>
                <td>{opp.metadata.ai_recommendation ?? '—'}</td>
                <td>{opp.metadata.deterministic_signal ?? '—'}</td>
                <td>
                  {opp.metadata.market_data_feed ?? '—'} ({opp.metadata.market_data_authority ?? '—'})
                </td>
                <td>{time(opp.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
