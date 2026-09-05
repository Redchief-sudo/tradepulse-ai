import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ScannerFunnel } from './ScannerFunnel'
import type { ScanRun } from '../types'

function run(overrides: Partial<ScanRun> = {}): ScanRun {
  return {
    scan_run_id: 'r1', scan_generation: 'gen-1', trigger: 'scheduled', asset_class: 'equity',
    status: 'completed', started_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:05:00Z',
    universe_size: 31, candidates_discovered: 5, candidates_approved: 2, orders_submitted: 1,
    error: null, market_data_tier: 'sip', equity_feed: 'sip', option_feed: null,
    ai_response_request_id: null, regime: null, regime_reason: null, regime_confidence: null,
    regime_position_multiplier: null, regime_realized_vol: null,
    ...overrides,
  }
}

describe('ScannerFunnel', () => {
  it('renders nothing (not a fabricated empty funnel) when no scan cycle exists for this lane', () => {
    const { container } = render(<ScannerFunnel laneKey="equity" label="Equity" run={undefined} filledCountByGeneration={{}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders every stage with real ScanRun counts once populated', () => {
    render(<ScannerFunnel laneKey="equity" label="Equity" run={run()} filledCountByGeneration={{ 'gen-1': 1 }} />)
    expect(screen.getByText('Configured Universe')).toBeInTheDocument()
    expect(screen.getByText('31')).toBeInTheDocument()
    expect(screen.getByText('Filled')).toBeInTheDocument()
  })

  it('shows "No data" for the Filled stage rather than fabricating 0 when the join has not resolved', () => {
    render(<ScannerFunnel laneKey="equity" label="Equity" run={run()} filledCountByGeneration={null} />)
    expect(screen.getByText('No data')).toBeInTheDocument()
  })
})
