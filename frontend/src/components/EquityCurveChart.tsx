import { useEffect, useRef } from 'react'
import { AreaSeries, createChart, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { money } from '../format'
import { Panel, EmptyState } from './Panel'

const MIN_POINTS_FOR_A_MEANINGFUL_CHART = 3

/** TradePulse persists an equity snapshot every scan cycle -- an intraday
 * series, often several observations per calendar day. A date-only time
 * key (slice(0, 10)) would collapse same-day snapshots onto one point and
 * violate lightweight-charts' strictly-ascending/unique time requirement;
 * this uses the full timestamp (seconds since epoch) so every persisted
 * snapshot gets its own point, deduplicating only the rare case of two
 * snapshots landing on the exact same second (keeping the later one). */
export function toChartPoints(data: { as_of: string; total_equity: string }[]): { time: UTCTimestamp; value: number }[] {
  const sorted = [...data].sort((a, b) => (a.as_of < b.as_of ? -1 : 1))
  const byTimestamp = new Map<number, number>()
  for (const snapshot of sorted) {
    const seconds = Math.floor(new Date(snapshot.as_of).getTime() / 1000)
    byTimestamp.set(seconds, Number(snapshot.total_equity))
  }
  return [...byTimestamp.entries()]
    .sort(([a], [b]) => a - b)
    .map(([time, value]) => ({ time: time as UTCTimestamp, value }))
}

export function EquityCurveChart() {
  const { data, error, loading } = usePolling(() => api.getEquityHistory(200), 60000)
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)

  const points = data ? toChartPoints(data) : []
  const hasEnoughHistory = points.length >= MIN_POINTS_FOR_A_MEANINGFUL_CHART

  useEffect(() => {
    if (!containerRef.current || !hasEnoughHistory) return
    const chart = createChart(containerRef.current, {
      height: 220,
      layout: { background: { color: 'transparent' }, textColor: '#8b93a7' },
      grid: { vertLines: { color: '#262b38' }, horzLines: { color: '#262b38' } },
      timeScale: { borderColor: '#262b38', timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#262b38' },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#4f8cff', topColor: 'rgba(79, 140, 255, 0.3)', bottomColor: 'rgba(79, 140, 255, 0.02)',
      priceLineVisible: false,
    })
    series.setData(points)
    chart.timeScale().fitContent()
    chartRef.current = chart

    const resize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
    }
    resize()
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      chart.remove()
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasEnoughHistory, points.length, points.map((p) => p.value).join(',')])

  const latest = data && data.length > 0 ? [...data].sort((a, b) => (a.as_of < b.as_of ? 1 : -1))[0] : null

  return (
    <Panel title="Equity Curve" error={error} loading={loading}>
      {!hasEnoughHistory && (
        <EmptyState>
          {points.length === 0
            ? 'No equity history yet -- this fills in as scan cycles run and persist snapshots.'
            : `Only ${points.length} snapshot(s) recorded so far -- not enough history yet for a meaningful chart.`}
        </EmptyState>
      )}
      {hasEnoughHistory && (
        <>
          <div ref={containerRef} />
          {latest && <p className="muted">Latest: {money(latest.total_equity)} as of {latest.as_of}</p>}
        </>
      )}
    </Panel>
  )
}
