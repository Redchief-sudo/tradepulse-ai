import { api } from '../api'
import { usePolling } from '../usePolling'
import { Panel } from './Panel'
import { LaneCard, LANES } from './LaneCard'
import { ScannerFunnel } from './ScannerFunnel'
import type { ScanRun } from '../types'

export function ScannerActivityPanel({ filledCountByGeneration }: { filledCountByGeneration: Record<string, number> | null }) {
  const { data, error, loading } = usePolling(() => api.getScanRuns(50), 20000)
  const { data: session } = usePolling(api.getSession, 5000)
  const { data: rejected } = usePolling(() => api.getRejectedCandidates(200), 30000)

  const latestByLane: Record<string, ScanRun> = {}
  if (data) {
    for (const run of data) {
      if (!(run.asset_class in latestByLane)) latestByLane[run.asset_class] = run
    }
  }

  const rejectedCountByLane: Record<string, number> | null = rejected
    ? rejected.reduce<Record<string, number>>((acc, r) => {
        acc[r.asset_class] = (acc[r.asset_class] ?? 0) + 1
        return acc
      }, {})
    : null

  const marketClosed = session?.state === 'market_closed'

  return (
    <Panel title="Scanner Activity" error={error} loading={loading}>
      <p className="muted">
        "AI candidates" are what the AI returned this cycle -- not a claim they all reached market-data evaluation.
        "Approved" candidates cleared the full deterministic/risk gate chain (see Recent Opportunities for their
        detail). "Rejected" counts the most recent persisted rejections for this lane, whatever reason they carry.
      </p>
      {LANES.map(({ key, label }) => (
        <div key={key}>
          <LaneCard
            laneKey={key} label={label} run={latestByLane[key]} marketClosed={marketClosed}
            rejectedCount={rejectedCountByLane ? (rejectedCountByLane[key] ?? 0) : null}
          />
          <ScannerFunnel laneKey={key} label={label} run={latestByLane[key]} filledCountByGeneration={filledCountByGeneration} />
        </div>
      ))}
    </Panel>
  )
}
