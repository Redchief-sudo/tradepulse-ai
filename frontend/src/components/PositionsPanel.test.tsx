import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PositionsPanel } from './PositionsPanel'
import type { EnrichedPosition } from '../types'
import { api } from '../api'

vi.mock('../api', () => ({ api: { getPositions: vi.fn() } }))

function position(overrides: Partial<EnrichedPosition['position']> = {}): EnrichedPosition {
  return {
    position: {
      symbol: 'AAPL', asset_class: 'equity', qty: '10', avg_entry_price: '100', market_value: '1050',
      current_price: '105', unrealized_pl: '50', ...overrides,
    },
    stop_loss: null, target_price: null, contract_multiplier: null,
  }
}

describe('PositionsPanel', () => {
  it('colors a positive unrealized P&L distinctly from a negative one', async () => {
    vi.mocked(api.getPositions).mockResolvedValue([
      position({ symbol: 'WINNER', unrealized_pl: '50' }),
      position({ symbol: 'LOSER', unrealized_pl: '-50', current_price: '95' }),
    ])
    render(<PositionsPanel />)

    await waitFor(() => expect(screen.getByText('WINNER')).toBeInTheDocument())
    const winnerRow = screen.getByText('WINNER').closest('tr')!
    const loserRow = screen.getByText('LOSER').closest('tr')!
    expect(winnerRow.querySelector('.positive')).toBeTruthy()
    expect(loserRow.querySelector('.negative')).toBeTruthy()
  })

  it('decodes an OCC option symbol into a readable contract label', async () => {
    vi.mocked(api.getPositions).mockResolvedValue([
      position({ symbol: 'AAPL240119C00150000', asset_class: 'option' }),
    ])
    render(<PositionsPanel />)

    await waitFor(() => expect(screen.getByText(/2024-01-19/)).toBeInTheDocument())
    expect(screen.getByText(/Call/)).toBeInTheDocument()
    expect(screen.getByText(/\$150\.00/)).toBeInTheDocument()
  })

  it('falls back to the raw symbol when OCC parsing does not match, never guessing a contract', async () => {
    vi.mocked(api.getPositions).mockResolvedValue([
      position({ symbol: 'NOT-A-VALID-OCC-SYMBOL', asset_class: 'option' }),
    ])
    render(<PositionsPanel />)

    await waitFor(() => expect(screen.getByText('NOT-A-VALID-OCC-SYMBOL')).toBeInTheDocument())
  })

  it('shows an empty state, not a fabricated table, when there are no open positions', async () => {
    vi.mocked(api.getPositions).mockResolvedValue([])
    render(<PositionsPanel />)

    await waitFor(() => expect(screen.getByText(/no open positions/i)).toBeInTheDocument())
  })
})
