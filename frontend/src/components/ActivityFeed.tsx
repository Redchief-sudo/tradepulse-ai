import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { AuditEvent, Fill, Opportunity, ReconciliationRecord, ScanRun, SettlementEvent, TradeIntent } from '../types'

type Category = 'SCAN' | 'SIGNAL' | 'ORDER' | 'FILL' | 'SETTLEMENT' | 'RECONCILIATION' | 'SYSTEM'

interface ActivityRow {
  key: string
  timestamp: string
  category: Category
  lane: string | null
  symbol: string | null
  message: string
}

function fromScanRuns(rows: ScanRun[]): ActivityRow[] {
  return rows.map((r) => ({
    key: `scan-${r.scan_run_id}`,
    timestamp: r.completed_at ?? r.started_at,
    category: 'SCAN',
    lane: r.asset_class,
    symbol: null,
    message: `${r.status} — universe ${r.universe_size}, ${r.candidates_discovered} candidates, ${r.candidates_approved} approved, ${r.orders_submitted} orders${r.error ? ` — ${r.error}` : ''}`,
  }))
}

function fromOpportunities(rows: Opportunity[]): ActivityRow[] {
  return rows.map((o) => ({
    key: `opp-${o.opportunity_id}`,
    timestamp: o.created_at,
    category: 'SIGNAL',
    lane: o.asset.asset_class,
    symbol: o.asset.symbol,
    message: `${o.metadata.deterministic_signal ?? 'signal unknown'} — AI: ${o.metadata.ai_recommendation ?? 'n/a'}`,
  }))
}

function fromTradeIntents(rows: TradeIntent[]): ActivityRow[] {
  return rows.map((i) => ({
    key: `intent-${i.trade_intent_id}`,
    timestamp: i.created_at,
    category: 'ORDER',
    lane: i.asset.asset_class,
    symbol: i.asset.symbol,
    message: `${i.side} ${i.status}${i.rejection_reason ? ` — ${i.rejection_reason}` : ''}`,
  }))
}

function fromFills(rows: Fill[]): ActivityRow[] {
  return rows.map((f) => ({
    key: `fill-${f.fill_id}`,
    timestamp: f.filled_at,
    category: 'FILL',
    lane: f.asset.asset_class,
    symbol: f.asset.symbol,
    message: `${f.side} ${f.quantity} @ ${f.price}`,
  }))
}

function fromSettlements(rows: SettlementEvent[]): ActivityRow[] {
  return rows.map((s) => ({
    key: `settle-${s.settlement_event_id}`,
    timestamp: s.occurred_at,
    category: 'SETTLEMENT',
    lane: s.asset.asset_class,
    symbol: s.asset.symbol,
    message: `${s.status}${s.error_code ? ` — ${s.error_code}` : ''}`,
  }))
}

function fromReconciliation(rows: ReconciliationRecord[]): ActivityRow[] {
  return rows.map((r) => ({
    key: `recon-${r.record_id}`,
    timestamp: r.occurred_at,
    category: 'RECONCILIATION',
    lane: null,
    symbol: r.subject_id,
    message: `${r.reconciliation_type}: ${r.outcome}${r.corrective_action ? ` — ${r.corrective_action}` : ''}`,
  }))
}

function fromAuditEvents(rows: AuditEvent[]): ActivityRow[] {
  return rows.map((e) => ({
    key: `audit-${e.event_id}`,
    timestamp: e.occurred_at,
    category: 'SYSTEM',
    lane: null,
    symbol: null,
    message: `${e.severity.toUpperCase()}: ${e.message}`,
  }))
}

const CATEGORY_TONE: Record<Category, string> = {
  SCAN: 'status-badge-neutral',
  SIGNAL: 'status-badge-hold',
  ORDER: 'status-badge-neutral',
  FILL: 'status-badge-ok',
  SETTLEMENT: 'status-badge-ok',
  RECONCILIATION: 'status-badge-warning',
  SYSTEM: 'status-badge-neutral',
}

/** Merges already-fetched, already-structured backend records
 * (scan-runs/opportunities/trade-intents/fills/settlements/audit-events)
 * into one chronological, categorized feed -- NOT log parsing, every row
 * traces to one already-typed backend record. Reused both as the compact
 * feed inside Scanner Activity (filtered to SCAN/SIGNAL) and as the full
 * dedicated Activity/Event Stream panel (all categories). Consumes the
 * lifted trade-lifecycle data as props so it doesn't re-poll
 * opportunities/trade-intents/fills/settlements a second time. */
export function ActivityFeed({
  title, categories, opportunities, tradeIntents, fills, settlements, limit = 30,
}: {
  title: string
  categories?: Category[]
  opportunities: Opportunity[] | null
  tradeIntents: TradeIntent[] | null
  fills: Fill[] | null
  settlements: SettlementEvent[] | null
  limit?: number
}) {
  const scanRuns = usePolling(() => api.getScanRuns(50), 20000)
  const auditEvents = usePolling(() => api.getAuditEvents(50), 30000)
  const reconciliation = usePolling(() => api.getReconciliation(50), 30000)

  const rows = [
    ...fromScanRuns(scanRuns.data ?? []),
    ...fromOpportunities(opportunities ?? []),
    ...fromTradeIntents(tradeIntents ?? []),
    ...fromFills(fills ?? []),
    ...fromSettlements(settlements ?? []),
    ...fromReconciliation(reconciliation.data ?? []),
    ...fromAuditEvents(auditEvents.data ?? []),
  ]
    .filter((r) => !categories || categories.includes(r.category))
    .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1))
    .slice(0, limit)

  const loading = scanRuns.loading || auditEvents.loading || reconciliation.loading
  const error = scanRuns.error ?? auditEvents.error ?? reconciliation.error

  return (
    <Panel title={title} error={error} loading={loading}>
      {rows.length === 0 && <EmptyState>No activity yet.</EmptyState>}
      {rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Category</th>
              <th>Lane</th>
              <th>Symbol</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{time(row.timestamp)}</td>
                <td>
                  <span className={`status-badge ${CATEGORY_TONE[row.category]}`}>{row.category}</span>
                </td>
                <td>{row.lane ?? '—'}</td>
                <td>{row.symbol ?? '—'}</td>
                <td>{row.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
