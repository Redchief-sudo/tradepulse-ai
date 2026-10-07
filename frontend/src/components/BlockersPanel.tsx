import { api } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel } from './Panel'

// Everything currently stopping trading, each with its cause, what it blocks,
// when it was last checked and how it clears (GET /api/blockers, read-only).
export function BlockersPanel() {
  const { data: report, error, loading } = usePolling(api.getBlockers, 10000)

  return (
    <Panel title="Blockers" error={error} loading={loading}>
      {report ? (
        <>
          <p className="muted">
            Last reconciliation pass: {report.last_reconciliation_at ? time(report.last_reconciliation_at) : 'never'}
          </p>
          {report.blockers.length === 0 ? (
            <div className="status-line status-line-ok">Nothing is blocking trading.</div>
          ) : (
            report.blockers.map((blocker, index) => (
              <div
                key={`${blocker.kind}-${blocker.subject}-${index}`}
                className={`status-line ${blocker.automatic ? 'status-line-unknown' : 'status-line-bad'}`}
              >
                <strong>
                  {blocker.subject}: {blocker.cause}
                </strong>
                <div>Blocks: {blocker.blocks}</div>
                <div className="muted">
                  Since {blocker.since ? time(blocker.since) : '?'} · last checked{' '}
                  {blocker.last_checked ? time(blocker.last_checked) : 'not yet'}
                </div>
                <div>
                  {blocker.automatic ? 'Clears automatically' : 'Needs operator'}: {blocker.resolution}
                </div>
              </div>
            ))
          )}
        </>
      ) : null}
    </Panel>
  )
}
