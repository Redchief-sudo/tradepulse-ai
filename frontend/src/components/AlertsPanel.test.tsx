import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AlertsPanel } from './AlertsPanel'
import { api } from '../api'

vi.mock('../api', () => ({ api: { getReconciliation: vi.fn(), getAuditEvents: vi.fn() } }))

const emptyPoll = { data: [], error: null, loading: false }
const loadingPoll = { data: null, error: null, loading: true }

describe('AlertsPanel / System Integrity', () => {
  it('never renders a healthy checkmark while settlement/trade-intent data has not loaded yet', async () => {
    vi.mocked(api.getReconciliation).mockResolvedValue([])
    vi.mocked(api.getAuditEvents).mockResolvedValue([])
    render(<AlertsPanel settlements={loadingPoll} tradeIntents={loadingPoll} />)

    await waitFor(() => expect(screen.getByText(/unresolved settlement errors/i)).toBeInTheDocument())
    const line = screen.getByText(/unresolved settlement errors/i).closest('.status-line')!
    expect(line.className).toMatch(/status-line-unknown/)
    expect(line.className).not.toMatch(/status-line-ok/)
  })

  it('renders unresolved settlement errors as a warning, not silently healthy', async () => {
    vi.mocked(api.getReconciliation).mockResolvedValue([])
    vi.mocked(api.getAuditEvents).mockResolvedValue([])
    render(
      <AlertsPanel
        settlements={{ data: [{ settlement_event_id: 's1', fill_id: 'f1', trade_intent_id: 't1', asset: { symbol: 'AAPL', asset_class: 'equity', native_asset_id: 'x', venue: null, metadata: {} }, side: 'buy', execution_mode: 'paper', quantity: '1', price: '100', status: 'terminal_failed', occurred_at: '2026-01-01T00:00:00Z', realized_pnl: null, error_code: 'BROKER_REJECTED' }], error: null, loading: false }}
        tradeIntents={emptyPoll}
      />,
    )

    await waitFor(() => expect(screen.getByText(/unresolved settlement errors/i)).toBeInTheDocument())
    const line = screen.getByText(/unresolved settlement errors/i).closest('.status-line')!
    expect(line.className).toMatch(/status-line-bad/)
  })

  it('reports a healthy state only once real data confirms zero issues', async () => {
    vi.mocked(api.getReconciliation).mockResolvedValue([])
    vi.mocked(api.getAuditEvents).mockResolvedValue([])
    render(<AlertsPanel settlements={emptyPoll} tradeIntents={emptyPoll} />)

    await waitFor(() => expect(screen.getByText(/unresolved settlement errors/i)).toBeInTheDocument())
    const line = screen.getByText(/unresolved settlement errors/i).closest('.status-line')!
    expect(line.className).toMatch(/status-line-ok/)
  })
})
