import { useState } from 'react'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel } from './Panel'

const PENDING_STATUSES = ['proposed', 'risk_approved', 'submitted', 'accepted', 'partially_filled', 'submission_unknown']

export function PendingOrdersPanel() {
  const [status, setStatus] = useState<string>('')
  const { data, error, loading } = usePolling(() => api.getTradeIntents(status || undefined), 10000)

  const shown = status ? data : data?.filter((intent) => PENDING_STATUSES.includes(intent.status))

  return (
    <Panel title="Trade Intents" error={error} loading={loading}>
      <div className="button-row">
        <label>
          Filter by status:{' '}
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Pending/unknown only</option>
            <option value="proposed">proposed</option>
            <option value="risk_approved">risk_approved</option>
            <option value="submitted">submitted</option>
            <option value="accepted">accepted</option>
            <option value="partially_filled">partially_filled</option>
            <option value="filled">filled</option>
            <option value="rejected">rejected</option>
            <option value="submission_unknown">submission_unknown</option>
          </select>
        </label>
      </div>
      {shown && shown.length === 0 && <p className="muted">Nothing to show.</p>}
      {shown && shown.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Status</th>
              <th>Requested</th>
              <th>Filled</th>
              <th>Created</th>
              <th>Sizing reasons</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((intent) => (
              <tr key={intent.trade_intent_id}>
                <td>{intent.asset.symbol}</td>
                <td>{intent.side}</td>
                <td>{intent.status}</td>
                <td>{intent.requested_quantity ?? '—'}</td>
                <td>{intent.filled_quantity}</td>
                <td>{time(intent.created_at)}</td>
                <td className="reasons">
                  {Array.isArray(intent.risk_snapshot?.reasons) ? (intent.risk_snapshot.reasons as string[]).join('; ') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
