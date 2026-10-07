import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { BlockersPanel } from './BlockersPanel'
import { api } from '../api'

vi.mock('../api', () => ({ api: { getBlockers: vi.fn() } }))

describe('BlockersPanel', () => {
  it('says plainly when nothing blocks trading', async () => {
    vi.mocked(api.getBlockers).mockResolvedValue({ last_reconciliation_at: '2026-10-06T15:00:00Z', blockers: [] })
    render(<BlockersPanel />)
    await waitFor(() => expect(screen.getByText(/nothing is blocking trading/i)).toBeInTheDocument())
  })

  it('shows each blocker with its cause, scope and resolution, marking operator action', async () => {
    vi.mocked(api.getBlockers).mockResolvedValue({
      last_reconciliation_at: null,
      blockers: [
        {
          kind: 'stranded_intent', subject: 'BTC/USD', cause: 'submission_unknown intent ti-1 cannot be proven',
          blocks: 'every order on this asset, protective exits included', since: '2026-10-06T14:00:00Z',
          last_checked: null, automatic: false, resolution: 'Check Alpaca for an order with this client_order_id.',
        },
      ],
    })
    render(<BlockersPanel />)
    await waitFor(() => expect(screen.getByText(/BTC\/USD: submission_unknown intent ti-1/)).toBeInTheDocument())
    expect(screen.getByText(/protective exits included/)).toBeInTheDocument()
    const line = screen.getByText(/Needs operator/).closest('.status-line')!
    expect(line.className).toMatch(/status-line-bad/)
    expect(screen.getByText(/Last reconciliation pass: never/)).toBeInTheDocument()
  })
})
