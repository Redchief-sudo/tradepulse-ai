import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from './App'
import { api } from './api'

vi.mock('./api', () => ({
  api: {
    getSession: vi.fn(),
    getMarketDataCapability: vi.fn(),
    getAccount: vi.fn(),
    getProvenance: vi.fn(),
    getRiskExposure: vi.fn(),
    getPnl: vi.fn(),
    getPositions: vi.fn(),
    getRiskLimits: vi.fn(),
    getScanRuns: vi.fn(),
    getRejectedCandidates: vi.fn(),
    getOpportunities: vi.fn(),
    getTradeIntents: vi.fn(),
    getFills: vi.fn(),
    getSettlements: vi.fn(),
    getReconciliation: vi.fn(),
    getAuditEvents: vi.fn(),
    getEquityHistory: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

function stubEverythingEmpty() {
  vi.mocked(api.getSession).mockResolvedValue({
    session_id: 's', state: 'active', trading_active: true, updated_at: '2026-01-01T00:00:00Z',
    kill_switch_reason: null, kill_switch_at: null, kill_switch_reset_required: false,
    financial_integrity_reason: null, financial_integrity_manual_reenable_required: false,
    execution_mode: 'paper', live_trading_enabled: false,
  })
  vi.mocked(api.getMarketDataCapability).mockResolvedValue({})
  vi.mocked(api.getAccount).mockResolvedValue({ equity: '100000', last_equity: '99000', cash: '50000', buying_power: '100000', portfolio_value: '100000' })
  vi.mocked(api.getProvenance).mockResolvedValue({
    product_name: 'x', creator_name: 'x', copyright_owner: 'x', company_name: 'x', copyright_years: 'x',
    software_version: '1', git_commit: 'abc', build_timestamp: 'x', provenance_version: '1', build_fingerprint: 'abcdef1234567890',
  })
  vi.mocked(api.getRiskExposure).mockResolvedValue({
    snapshot_id: 's', as_of: '2026-01-01T00:00:00Z', total_equity: '100000', cash_balance: '50000',
    holdings_value: '50000', sector_exposure: {}, open_positions: 0, outstanding_orders: 0, trades_today: 0,
    daily_pnl_pct: '0', source: 'broker',
  })
  vi.mocked(api.getPnl).mockResolvedValue({ realized: [], unrealized_total: '0', positions_unrealized: [] })
  vi.mocked(api.getPositions).mockResolvedValue([])
  vi.mocked(api.getRiskLimits).mockResolvedValue({
    profile_id: 'balanced', max_total_exposure_pct: '40', max_sector_pct: '20', max_position_pct: '7',
    max_daily_trades: 3, max_open_positions: 5, max_drawdown_pct: '15', max_daily_loss_pct: '5',
  })
  vi.mocked(api.getScanRuns).mockResolvedValue([])
  vi.mocked(api.getRejectedCandidates).mockResolvedValue([])
  vi.mocked(api.getOpportunities).mockResolvedValue([])
  vi.mocked(api.getTradeIntents).mockResolvedValue([])
  vi.mocked(api.getFills).mockResolvedValue([])
  vi.mocked(api.getSettlements).mockResolvedValue([])
  vi.mocked(api.getReconciliation).mockResolvedValue([])
  vi.mocked(api.getAuditEvents).mockResolvedValue([])
  vi.mocked(api.getEquityHistory).mockResolvedValue([])
}

describe('App', () => {
  it('renders the full dashboard without crashing, at both a wide and a narrow viewport', async () => {
    stubEverythingEmpty()
    render(<App />)

    await waitFor(() => expect(screen.getByText('TradePulse')).toBeInTheDocument())
    expect(screen.getByText('Scanner Activity')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Open Positions' })).toBeInTheDocument()
    expect(screen.getByText('Trade Lifecycle')).toBeInTheDocument()

    // Simulate a narrow (mobile-ish) viewport -- the layout must not throw
    // or remove any financial panel merely because the width changed.
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 480 })
    window.dispatchEvent(new Event('resize'))
    expect(screen.getByText('Scanner Activity')).toBeInTheDocument()
    expect(screen.getByText('Risk Exposure')).toBeInTheDocument()
  })

  it('shows an honest "not enough history" state instead of fabricating an equity chart', async () => {
    stubEverythingEmpty()
    render(<App />)

    await waitFor(() => expect(screen.getByText(/no equity history yet/i)).toBeInTheDocument())
  })
})
