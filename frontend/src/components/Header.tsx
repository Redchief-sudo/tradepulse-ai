import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePolling } from '../usePolling'
import { relativeAgo } from '../format'
import type { SessionState } from '../types'

const SESSION_STATE_LABEL: Record<SessionState, string> = {
  disabled: 'Disabled',
  active: 'Active',
  risk_stopped: 'Risk Stopped',
  system_degraded: 'System Degraded',
  broker_unavailable: 'Broker Unavailable',
  market_closed: 'Market Closed',
  manually_stopped: 'Manually Stopped',
  financial_integrity_blocked: 'Financial Integrity Blocked',
}

function sessionPillTone(state: SessionState | undefined): 'positive' | 'negative' | 'warning' | 'neutral' {
  if (!state) return 'neutral'
  if (state === 'active') return 'positive'
  if (state === 'risk_stopped' || state === 'financial_integrity_blocked' || state === 'broker_unavailable') return 'negative'
  if (state === 'market_closed' || state === 'system_degraded') return 'warning'
  return 'neutral'
}

/** "Market state" is a coarser, display-only derivation of the same session
 * state SessionPanel already shows verbatim -- it exists here only to avoid
 * a header reading "Market Closed" in a way that implies crypto is halted
 * too, per the verified execution_session_decision/is_continuous_market
 * exemption (see ScannerActivityPanel for the authoritative per-lane
 * rendering of this same rule). */
function marketStateLabel(state: SessionState | undefined): { label: string; tone: 'positive' | 'negative' | 'warning' | 'neutral' } {
  if (state === 'active') return { label: 'Market Session Open', tone: 'positive' }
  if (state === 'market_closed') return { label: 'Equity/Options Closed — Crypto Active', tone: 'warning' }
  if (!state) return { label: 'Market Status Unknown', tone: 'neutral' }
  return { label: 'Market Status Unknown', tone: 'neutral' }
}

function StatusPill({ label, tone }: { label: string; tone: 'positive' | 'negative' | 'warning' | 'neutral' }) {
  return (
    <span className={`status-pill status-pill-${tone}`}>
      <span className="status-pill-dot" />
      {label}
    </span>
  )
}

export function Header() {
  const { data: session } = usePolling(api.getSession, 5000)
  const { data: capability } = usePolling(api.getMarketDataCapability, 30000)
  const [lastRefreshIso, setLastRefreshIso] = useState<string | null>(null)

  useEffect(() => {
    if (session) setLastRefreshIso(new Date().toISOString())
  }, [session])

  const sessionTone = sessionPillTone(session?.state)
  const market = marketStateLabel(session?.state)
  const tradingTone = session ? (session.trading_active ? 'positive' : 'negative') : 'neutral'
  const integrityTone = session ? (session.state === 'financial_integrity_blocked' ? 'negative' : 'positive') : 'neutral'

  const tierEntries = capability ? Object.entries(capability) : []
  const tierLabel =
    tierEntries.length === 0
      ? 'no data yet'
      : tierEntries.map(([lane, run]) => `${lane}: ${run.market_data_tier ?? '—'}`).join(' · ')

  return (
    <header className="app-header">
      <div className="app-header-top">
        <h1>TradePulse</h1>
        <span className="app-header-subtitle">
          Local operator view -- bound to 127.0.0.1 only. No remote access, no authentication (phase 1).
        </span>
      </div>
      <div className="status-pills">
        <StatusPill label={session ? SESSION_STATE_LABEL[session.state] : 'Session Unknown'} tone={sessionTone} />
        <StatusPill label={market.label} tone={market.tone} />
        <StatusPill label={tradingTone === 'positive' ? 'Trading Active' : 'Trading Halted'} tone={session ? tradingTone : 'neutral'} />
        <StatusPill label={`Market Data: ${tierLabel}`} tone="neutral" />
        <StatusPill label={integrityTone === 'positive' ? 'Integrity OK' : 'Integrity Blocked'} tone={session ? integrityTone : 'neutral'} />
        <StatusPill label={lastRefreshIso ? `Refreshed ${relativeAgo(lastRefreshIso)}` : 'Refreshed —'} tone="neutral" />
      </div>
    </header>
  )
}
