import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel } from './Panel'

export function AlertsPanel() {
  const reconciliation = usePolling(() => api.getReconciliation(20), 30000)
  const audit = usePolling(() => api.getAuditEvents(20), 30000)

  const notableReconciliation = reconciliation.data?.filter((r) => r.outcome !== 'matched') ?? []
  const notableAudit = audit.data?.filter((e) => e.severity === 'warning' || e.severity === 'error' || e.severity === 'critical') ?? []

  return (
    <Panel title="Reconciliation &amp; Audit Alerts" error={reconciliation.error ?? audit.error} loading={reconciliation.loading || audit.loading}>
      <h3>Reconciliation drift/corrections</h3>
      {notableReconciliation.length === 0 && <p className="muted">No drift or corrections in the recent window.</p>}
      {notableReconciliation.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Subject</th>
              <th>Outcome</th>
              <th>Corrective action</th>
              <th>Occurred</th>
            </tr>
          </thead>
          <tbody>
            {notableReconciliation.map((r) => (
              <tr key={r.record_id} className="row-danger">
                <td>{r.reconciliation_type}</td>
                <td>{r.subject_id}</td>
                <td>{r.outcome}</td>
                <td>{r.corrective_action ?? '—'}</td>
                <td>{time(r.occurred_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Audit events (warning+)</h3>
      {notableAudit.length === 0 && <p className="muted">Nothing above info severity in the recent window.</p>}
      {notableAudit.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Type</th>
              <th>Message</th>
              <th>Occurred</th>
            </tr>
          </thead>
          <tbody>
            {notableAudit.map((e) => (
              <tr key={e.event_id} className={e.severity === 'critical' || e.severity === 'error' ? 'row-danger' : 'row-warning'}>
                <td>{e.severity}</td>
                <td>{e.event_type}</td>
                <td>{e.message}</td>
                <td>{time(e.occurred_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
