import { describe, expect, it } from 'vitest'
import { chartRefreshKey, toChartPoints } from './EquityCurveChart'

describe('toChartPoints', () => {
  it('gives every intraday snapshot its own distinct time key, never collapsing same-day scans onto one point', () => {
    const points = toChartPoints([
      { as_of: '2026-09-04T08:30:00Z', total_equity: '100000' },
      { as_of: '2026-09-04T12:45:00Z', total_equity: '100500' },
      { as_of: '2026-09-04T16:00:00Z', total_equity: '99800' },
    ])
    expect(points).toHaveLength(3)
    const uniqueTimes = new Set(points.map((p) => p.time))
    expect(uniqueTimes.size).toBe(3)
  })

  it('produces strictly ascending time values, as lightweight-charts requires', () => {
    const points = toChartPoints([
      { as_of: '2026-09-04T16:00:00Z', total_equity: '99800' },
      { as_of: '2026-09-03T08:30:00Z', total_equity: '100000' },
      { as_of: '2026-09-04T08:30:00Z', total_equity: '100000' },
    ])
    for (let i = 1; i < points.length; i++) {
      expect(points[i].time).toBeGreaterThan(points[i - 1].time)
    }
  })

  it('deduplicates two snapshots landing on the exact same second by keeping the later value', () => {
    const points = toChartPoints([
      { as_of: '2026-09-04T08:30:00Z', total_equity: '100000' },
      { as_of: '2026-09-04T08:30:00Z', total_equity: '100050' },
    ])
    expect(points).toHaveLength(1)
    expect(points[0].value).toBe(100050)
  })
})

describe('chartRefreshKey', () => {
  it('changes when a rolling window shifts even though the value sequence is unchanged (UI-092-01)', () => {
    // Equity flat at 500000 across the window boundary -- oldest point
    // drops, a new one is appended, length stays constant, and every
    // value in the sequence is identical before and after. Only the
    // timestamps differ. A value-only dependency key would (wrongly)
    // stay the same here and leave the chart showing a stale time axis.
    const before = toChartPoints([
      { as_of: '2026-09-01T00:00:00Z', total_equity: '500000' },
      { as_of: '2026-09-02T00:00:00Z', total_equity: '500000' },
      { as_of: '2026-09-03T00:00:00Z', total_equity: '500000' },
    ])
    const after = toChartPoints([
      { as_of: '2026-09-02T00:00:00Z', total_equity: '500000' },
      { as_of: '2026-09-03T00:00:00Z', total_equity: '500000' },
      { as_of: '2026-09-04T00:00:00Z', total_equity: '500000' },
    ])
    expect(before.map((p) => p.value)).toEqual(after.map((p) => p.value)) // identical value sequence
    expect(chartRefreshKey(before)).not.toBe(chartRefreshKey(after)) // but the key must still differ
  })

  it('changes when any single value changes', () => {
    const before = toChartPoints([{ as_of: '2026-09-04T08:30:00Z', total_equity: '100000' }])
    const after = toChartPoints([{ as_of: '2026-09-04T08:30:00Z', total_equity: '100050' }])
    expect(chartRefreshKey(before)).not.toBe(chartRefreshKey(after))
  })
})
