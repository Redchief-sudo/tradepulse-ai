import { useEffect, useRef } from 'react'
import { AreaSeries, createChart, type IChartApi } from 'lightweight-charts'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { money } from '../format'
import { Panel, EmptyState } from './Panel'

const MIN_POINTS_FOR_A_MEANINGFUL_CHART = 3

export function EquityCurveChart() {
  const { data, error, loading } = usePolling(() => api.getEquityHistory(200), 60000)
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)

  const points = data
    ? [...data]
        .sort((a, b) => (a.as_of < b.as_of ? -1 : 1))
        .map((snapshot) => ({ time: snapshot.as_of.slice(0, 10), value: Number(snapshot.total_equity) }))
    : []
  const hasEnoughHistory = points.length >= MIN_POINTS_FOR_A_MEANINGFUL_CHART

  useEffect(() => {
    if (!containerRef.current || !hasEnoughHistory) return
    const chart = createChart(containerRef.current, {
      height: 220,
      layout: { background: { color: 'transparent' }, textColor: '#8b93a7' },
      grid: { vertLines: { color: '#262b38' }, horzLines: { color: '#262b38' } },
      timeScale: { borderColor: '#262b38' },
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
