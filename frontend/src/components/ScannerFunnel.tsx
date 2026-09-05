import type { ScanRun } from '../types'

interface FunnelStage {
  label: string
  count: number | null
}

function FunnelBar({ stage, maxCount }: { stage: FunnelStage; maxCount: number }) {
  const pct = stage.count !== null && maxCount > 0 ? Math.max(2, (stage.count / maxCount) * 100) : 0
  return (
    <div className="funnel-row">
      <span className="funnel-label">{stage.label}</span>
      <span className="funnel-track">
        <span className="funnel-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="funnel-count num">{stage.count === null ? 'No data' : stage.count}</span>
    </div>
  )
}

/** Universe -> Scanned -> AI Candidates -> Approved -> Orders -> Filled,
 * per lane. Every count comes straight from ScanRun's already-persisted
 * fields except "Filled," which comes from the shared trade-lifecycle join
 * (see useTradeLifecycleData) -- matched by scan_generation, never a new
 * computation of its own. A stage with genuinely no data reads "No data,"
 * never a fabricated 0. */
export function ScannerFunnel({
  laneKey, label, run, filledCountByGeneration,
}: {
  laneKey: string
  label: string
  run: ScanRun | undefined
  filledCountByGeneration: Record<string, number> | null
}) {
  if (!run) return null
  const filled = filledCountByGeneration ? (filledCountByGeneration[run.scan_generation] ?? 0) : null
  const stages: FunnelStage[] = [
    { label: 'Configured Universe', count: run.universe_size },
    { label: 'AI Candidates', count: run.candidates_discovered },
    { label: 'Deterministically Approved', count: run.candidates_approved },
    { label: 'Orders Submitted', count: run.orders_submitted },
    { label: 'Filled', count: filled },
  ]
  const maxCount = Math.max(1, run.universe_size)
  return (
    <div className="funnel" data-lane={laneKey}>
      <div className="funnel-title">{label} funnel</div>
      {stages.map((stage) => (
        <FunnelBar key={stage.label} stage={stage} maxCount={maxCount} />
      ))}
    </div>
  )
}
