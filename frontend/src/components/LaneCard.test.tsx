import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LaneCard } from './LaneCard'
import type { ScanRun } from '../types'

function baseRun(overrides: Partial<ScanRun> = {}): ScanRun {
  return {
    scan_run_id: 'run-1', scan_generation: 'gen-1', trigger: 'scheduled', asset_class: 'equity',
    status: 'completed', started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:05:00Z',
    universe_size: 31, candidates_discovered: 5, candidates_approved: 2, orders_submitted: 1,
    error: null, market_data_tier: 'sip', equity_feed: 'sip', option_feed: null,
    ai_response_request_id: null, regime: null, regime_reason: null, regime_confidence: null,
    regime_position_multiplier: null, regime_realized_vol: null,
    ...overrides,
  }
}

describe('LaneCard', () => {
  it('renders an empty state when no scan cycle has ever run for this lane', () => {
    render(<LaneCard laneKey="equity" label="Equity" run={undefined} marketClosed={false} rejectedCount={null} />)
    expect(screen.getByText(/no scan cycle recorded yet/i)).toBeInTheDocument()
  })

  it('visually distinguishes a genuinely classified regime from an unavailable one', () => {
    const { rerender } = render(
      <LaneCard laneKey="equity" label="Equity" run={baseRun({ regime: 'low_vol_bull', regime_position_multiplier: '0.90' })} marketClosed={false} rejectedCount={3} />,
    )
    const classified = screen.getByText(/LOW_VOL_BULL/)
    expect(classified.className).not.toMatch(/regime-block-unavailable/)
    expect(classified.textContent).toContain('0.90x')

    rerender(
      <LaneCard
        laneKey="equity" label="Equity"
        run={baseRun({ regime: 'unavailable', regime_reason: 'benchmark fetch failed', regime_position_multiplier: '0.50' })}
        marketClosed={false} rejectedCount={3}
      />,
    )
    const unavailable = screen.getByText(/REGIME UNAVAILABLE/)
    expect(unavailable.className).toMatch(/regime-block-unavailable/)
    expect(unavailable.textContent).toContain('FALLBACK 0.50x')
  })

  it('shows WAITING -- MARKET CLOSED for equity when the market is closed, never for crypto', () => {
    const { rerender } = render(<LaneCard laneKey="equity" label="Equity" run={baseRun()} marketClosed={true} rejectedCount={0} />)
    expect(screen.getByText(/WAITING/)).toBeInTheDocument()

    rerender(<LaneCard laneKey="crypto" label="Crypto" run={baseRun({ asset_class: 'crypto', status: 'running' })} marketClosed={true} rejectedCount={0} />)
    expect(screen.queryByText(/WAITING/)).not.toBeInTheDocument()
    expect(screen.getByText(/ACTIVE — SCANNING/)).toBeInTheDocument()
  })

  it('renders rejected count as Unavailable, never a fabricated 0, when the rejected-candidates fetch hasn\'t resolved', () => {
    render(<LaneCard laneKey="equity" label="Equity" run={baseRun()} marketClosed={false} rejectedCount={null} />)
    expect(screen.getByText('Unavailable')).toBeInTheDocument()
  })
})
