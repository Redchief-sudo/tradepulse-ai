import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RiskExposurePanel } from './RiskExposurePanel'
import { api } from '../api'
import type { EnrichedPosition, PortfolioSnapshot, RiskLimits } from '../types'

vi.mock('../api', () => ({
  api: { getRiskExposure: vi.fn(), getRiskLimits: vi.fn(), getPositions: vi.fn() },
}))

const RISK_EXPOSURE: PortfolioSnapshot = {
  snapshot_id: 's', as_of: '2026-01-01T00:00:00Z', total_equity: '100000', cash_balance: '50000',
  holdings_value: '50000', sector_exposure: {}, open_positions: 1, outstanding_orders: 0, trades_today: 0,
  daily_pnl_pct: '0', source: 'broker',
}
const RISK_LIMITS: RiskLimits = {
  profile_id: 'balanced', max_total_exposure_pct: '40', max_sector_pct: '20', max_position_pct: '7',
  max_daily_trades: 3, max_open_positions: 5, max_drawdown_pct: '15', max_daily_loss_pct: '5',
}

function position(overrides: Partial<EnrichedPosition['position']> = {}, contract_multiplier: string | null = null): EnrichedPosition {
  return {
    position: {
      symbol: 'AAPL', asset_class: 'equity', qty: '10', avg_entry_price: '100', market_value: '1000',
      current_price: '100', unrealized_pl: '0', ...overrides,
    },
    stop_loss: null, target_price: null, contract_multiplier,
  }
}

describe('RiskExposurePanel position-cap utilization', () => {
  it('applies the option contract multiplier instead of understating notional by 100x', async () => {
    vi.mocked(api.getRiskExposure).mockResolvedValue(RISK_EXPOSURE)
    vi.mocked(api.getRiskLimits).mockResolvedValue(RISK_LIMITS)
    // 2 contracts @ $3 premium, multiplier 100 -> true notional $600, not $6.
    // max_position_pct=7% of $100,000 equity = $7,000 limit -> utilization ~8.57%.
    vi.mocked(api.getPositions).mockResolvedValue([
      position({ symbol: 'AAPL240119C00150000', asset_class: 'option', qty: '2', current_price: '3' }, '100'),
    ])
    render(<RiskExposurePanel />)

    await waitFor(() => expect(screen.getByText('AAPL240119C00150000')).toBeInTheDocument())
    const row = screen.getByText('AAPL240119C00150000').closest('.util-bar-row')!
    expect(row.querySelector('.util-bar-pct')?.textContent).toBe('9%') // 600/7000 rounded
  })

  it('shows real utilization magnitude for a short position instead of clamping to 0%', async () => {
    vi.mocked(api.getRiskExposure).mockResolvedValue(RISK_EXPOSURE)
    vi.mocked(api.getRiskLimits).mockResolvedValue(RISK_LIMITS)
    // -50 shares @ $100 -> abs notional $5,000 against a $7,000 limit = ~71%.
    vi.mocked(api.getPositions).mockResolvedValue([
      position({ symbol: 'SHORTCO', qty: '-50', current_price: '100' }),
    ])
    render(<RiskExposurePanel />)

    await waitFor(() => expect(screen.getByText('SHORTCO')).toBeInTheDocument())
    const row = screen.getByText('SHORTCO').closest('.util-bar-row')!
    expect(row.querySelector('.util-bar-pct')?.textContent).toBe('71%')
  })
})
