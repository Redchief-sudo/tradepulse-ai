import { money, time } from '../format'
import { Panel, EmptyState } from './Panel'
import type { LifecycleEntry } from '../useTradeLifecycleData'

/** Opportunity -> Intent -> Order -> Fill -> Settlement -> realized P&L,
 * joined purely from already-authoritative IDs (see useTradeLifecycleData).
 * "No opportunity" is a normal, correctly-labeled case for a monitor-
 * originated exit (a stop-loss/target close never had a scanner
 * Opportunity to begin with) -- never rendered as an error or dropped. */
export function TradeLifecyclePanel({
  entries, loading, error,
}: {
  entries: LifecycleEntry[]
  loading: boolean
  error: string | null
}) {
  const sorted = [...entries].sort((a, b) => (a.tradeIntent.created_at < b.tradeIntent.created_at ? 1 : -1)).slice(0, 30)

  return (
    <Panel title="Trade Lifecycle" error={error} loading={loading}>
      <p className="muted">
        Opportunity → Intent → Order → Fill → Settlement, joined by the backend's own IDs (correlation_id / trade_intent_id
        / fill_id) -- never a new financial computation. A monitor-originated exit (stop-loss/target close) correctly
        shows "no opportunity," not an error.
      </p>
      {sorted.length === 0 && <EmptyState>No trade activity yet.</EmptyState>}
      {sorted.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Created</th>
              <th>Opportunity</th>
              <th>Intent status</th>
              <th>Fills</th>
              <th>Settlement</th>
              <th className="num">Realized P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((entry) => {
              const realized = entry.settlements.reduce(
                (sum, s) => (s.realized_pnl !== null ? sum + Number(s.realized_pnl) : sum), 0,
              )
              const hasRealized = entry.settlements.some((s) => s.realized_pnl !== null)
              const settlementSummary = entry.settlements.length === 0 ? 'Not settled yet' : entry.settlements.map((s) => s.status).join(', ')
              return (
                <tr key={entry.tradeIntent.trade_intent_id}>
                  <td>{entry.tradeIntent.asset.symbol}</td>
                  <td>{time(entry.tradeIntent.created_at)}</td>
                  <td>{entry.opportunity ? entry.opportunity.metadata.deterministic_signal ?? 'yes' : 'No opportunity (monitor exit)'}</td>
                  <td>{entry.tradeIntent.status}</td>
                  <td>{entry.fills.length === 0 ? 'No fills yet' : `${entry.fills.length} fill(s)`}</td>
                  <td>{settlementSummary}</td>
                  <td className={`num${hasRealized ? (realized >= 0 ? ' positive' : ' negative') : ''}`}>
                    {hasRealized ? money(realized) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
