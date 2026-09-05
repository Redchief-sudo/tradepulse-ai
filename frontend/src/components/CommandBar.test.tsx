import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CommandBar } from './CommandBar'
import { api } from '../api'
import type { TradingSession } from '../types'

vi.mock('../api', () => ({
  api: {
    getSession: vi.fn(),
    getMarketDataCapability: vi.fn(),
    getAccount: vi.fn(),
    getProvenance: vi.fn(),
  },
}))

function session(overrides: Partial<TradingSession> = {}): TradingSession {
  return {
    session_id: 's', state: 'active', trading_active: true, updated_at: '2026-01-01T00:00:00Z',
    kill_switch_reason: null, kill_switch_at: null, kill_switch_reset_required: false,
    financial_integrity_reason: null, financial_integrity_manual_reenable_required: false,
    execution_mode: 'paper', live_trading_enabled: false,
    ...overrides,
  }
}

describe('CommandBar', () => {
  it('renders a visually distinct LIVE badge, never the same as PAPER', async () => {
    vi.mocked(api.getSession).mockResolvedValue(session({ execution_mode: 'live', live_trading_enabled: true }))
    vi.mocked(api.getMarketDataCapability).mockResolvedValue({})
    vi.mocked(api.getAccount).mockRejectedValue(new Error('unused in this test'))
    vi.mocked(api.getProvenance).mockResolvedValue({
      product_name: 'x', creator_name: 'x', copyright_owner: 'x', company_name: 'x', copyright_years: 'x',
      software_version: '1', git_commit: 'abc', build_timestamp: 'x', provenance_version: '1', build_fingerprint: 'abcdef1234567890',
    })
    render(<CommandBar />)

    await waitFor(() => expect(screen.getByText('LIVE')).toBeInTheDocument())
    expect(screen.getByText('LIVE').className).toMatch(/mode-badge-live/)
    expect(screen.queryByText('PAPER')).not.toBeInTheDocument()
  })

  it('renders PAPER with a distinct, non-LIVE badge class', async () => {
    vi.mocked(api.getSession).mockResolvedValue(session())
    vi.mocked(api.getMarketDataCapability).mockResolvedValue({})
    vi.mocked(api.getAccount).mockRejectedValue(new Error('unused in this test'))
    vi.mocked(api.getProvenance).mockResolvedValue({
      product_name: 'x', creator_name: 'x', copyright_owner: 'x', company_name: 'x', copyright_years: 'x',
      software_version: '1', git_commit: 'abc', build_timestamp: 'x', provenance_version: '1', build_fingerprint: 'abcdef1234567890',
    })
    render(<CommandBar />)

    await waitFor(() => expect(screen.getByText('PAPER')).toBeInTheDocument())
    expect(screen.getByText('PAPER').className).toMatch(/mode-badge-paper/)
    expect(screen.getByText('PAPER').className).not.toMatch(/mode-badge-live/)
  })

  it('renders broker connectivity as unavailable, never connected, on a failed account poll', async () => {
    vi.mocked(api.getSession).mockResolvedValue(session())
    vi.mocked(api.getMarketDataCapability).mockResolvedValue({})
    vi.mocked(api.getAccount).mockRejectedValue(new Error('BROKER_UNAVAILABLE: timeout'))
    vi.mocked(api.getProvenance).mockResolvedValue({
      product_name: 'x', creator_name: 'x', copyright_owner: 'x', company_name: 'x', copyright_years: 'x',
      software_version: '1', git_commit: 'abc', build_timestamp: 'x', provenance_version: '1', build_fingerprint: 'abcdef1234567890',
    })
    render(<CommandBar />)

    await waitFor(() => expect(screen.getByText(/Broker Unavailable/)).toBeInTheDocument())
  })

  it('never keeps showing Broker Connected once a later poll fails, even though stale account data is intentionally retained', async () => {
    // usePolling deliberately keeps the last-good `data` on a failed
    // refresh -- CommandBar must still prioritize the MOST RECENT poll's
    // `error` over that stale `data` when deciding connectivity, not read
    // `data` first and never notice the newest request actually failed.
    vi.useFakeTimers()
    try {
      vi.mocked(api.getSession).mockResolvedValue(session())
      vi.mocked(api.getMarketDataCapability).mockResolvedValue({})
      vi.mocked(api.getProvenance).mockResolvedValue({
        product_name: 'x', creator_name: 'x', copyright_owner: 'x', company_name: 'x', copyright_years: 'x',
        software_version: '1', git_commit: 'abc', build_timestamp: 'x', provenance_version: '1', build_fingerprint: 'abcdef1234567890',
      })
      vi.mocked(api.getAccount)
        .mockResolvedValueOnce({ equity: '100000', last_equity: '99000', cash: '50000', buying_power: '100000', portfolio_value: '100000' })
        .mockRejectedValueOnce(new Error('BROKER_UNAVAILABLE: timeout'))

      render(<CommandBar />)
      await vi.waitFor(() => expect(screen.getByText(/Broker Connected/)).toBeInTheDocument())

      await vi.advanceTimersByTimeAsync(10000) // the account poll's own interval
      await vi.waitFor(() => expect(screen.getByText(/Broker Unavailable/)).toBeInTheDocument())
      expect(screen.queryByText(/Broker Connected/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
