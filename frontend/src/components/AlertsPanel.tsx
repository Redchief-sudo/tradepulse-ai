import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { SettlementEvent, TradeIntent } from '../types'

type LineState = 'ok' | 'bad' | 'unknown'

function SummaryLine({ label, state, detail }: { label: string; state: LineState; detail?: string }) {
  const icon = state === 'ok' ? '✓' : state === 'bad' ? '⚠' : '?'
  return (
    <div className={`status-line status-line-${state}`}>
      <span>{icon}</span>
      <span>
        {label}
        {detail ? ` — ${detail}` : ''}
      </span>
    </div>
  )
}

export function AlertsPanel({
  settlements, tradeIntents,
}: {
  settlements: { data: SettlementEvent[] | null; error: string | null; loading: boolean }
  tradeIntents: { data: TradeIntent[] | null; error: string | null; loading: boolean }
}) {
  const reconciliation = usePolling(() => api.getReconciliation(20), 30000)
  const audit = usePolling(() => api.getAuditEvents(20), 30000)

  const notableReconciliation = reconciliation.data?.filter((r) => r.outcome !== 'matched') ?? []
  const notableAudit = audit.data?.filter((e) => e.severity === 'warning' || e.severity === 'error' || e.severity === 'critical') ?? []

  const driftRecords = reconciliation.data?.filter((r) => r.outcome === 'drift_detected') ?? []
  const fillDrift = driftRecords.filter((r) => r.reconciliation_type === 'fill')
  const criticalAudit = audit.data?.filter((e) => e.severity === 'critical') ?? []
  const unresolvedSettlementErrors = settlements.data?.filter((s) => s.error_code !== null) ?? []
  const submissionUnknown = tradeIntents.data?.filter((i) => i.status === 'submission_unknown') ?? []

  // A fetch error must render as unknown, never as a green checkmark --
  // checked before any healthy/unhealthy line is decided.
  const reconciliationLineState = (ok: boolean): LineState => (reconciliation.error ? 'unknown' : ok ? 'ok' : 'bad')
  const auditLineState = (ok: boolean): LineState => (audit.error ? 'unknown' : ok ? 'ok' : 'bad')
  const settlementLineState = (ok: boolean): LineState => (settlements.error ? 'unknown' : settlements.data === null ? 'unknown' : ok ? 'ok' : 'bad')
  const intentLineState = (ok: boolean): LineState => (tradeIntents.error ? 'unknown' : tradeIntents.data === null ? 'unknown' : ok ? 'ok' : 'bad')

  const overallError = reconciliation.error ?? audit.error ?? settlements.error ?? tradeIntents.error
  const overallLoading = reconciliation.loading || audit.loading || settlements.loading || tradeIntents.loading

  return (
    <Panel title="System Integrity" error={overallError} loading={overallLoading}>
      <h3>System integrity</h3>
      <SummaryLine
        label="Position/view reconciliation"
        state={reconciliationLineState(driftRecords.length === 0)}
        detail={driftRecords.length > 0 ? `${driftRecords.length} drift event(s)` : undefined}
      />
      <SummaryLine
        label="Fill/settlement reconciliation"
        state={reconciliationLineState(fillDrift.length === 0)}
        detail={fillDrift.length > 0 ? `${fillDrift.length} drift event(s)` : undefined}
      />
      <SummaryLine
        label="Integrity-critical audit events"
        state={auditLineState(criticalAudit.length === 0)}
        detail={criticalAudit.length > 0 ? `${criticalAudit.length} critical event(s)` : undefined}
      />
      <SummaryLine
        label="Unresolved settlement errors"
        state={settlementLineState(unresolvedSettlementErrors.length === 0)}
        detail={unresolvedSettlementErrors.length > 0 ? `${unresolvedSettlementErrors.length} error(s)` : undefined}
      />
      <SummaryLine
        label="Submission-unknown trade intents"
        state={intentLineState(submissionUnknown.length === 0)}
        detail={submissionUnknown.length > 0 ? `${submissionUnknown.length} unresolved` : undefined}
      />

      <h3>Reconciliation drift/corrections</h3>
      {notableReconciliation.length === 0 && <EmptyState>No drift or corrections in the recent window.</EmptyState>}
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
      {notableAudit.length === 0 && <EmptyState>Nothing above info severity in the recent window.</EmptyState>}
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
