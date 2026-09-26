import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePolling } from '../usePolling'
import { time, duration } from '../format'
import { Panel } from './Panel'
import type { SessionState } from '../types'

const STATE_LABEL: Record<SessionState, string> = {
  disabled: 'Disabled',
  active: 'Active',
  risk_stopped: 'Risk Stopped',
  system_degraded: 'System Degraded',
  broker_unavailable: 'Broker Unavailable',
  market_closed: 'Market Closed',
  manually_stopped: 'Manually Stopped',
  financial_integrity_blocked: 'Financial Integrity Blocked',
}

export function SessionPanel() {
  const { data: session, error, loading, refresh } = usePolling(api.getSession, 5000)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setActionError(null)
    try {
      await action()
      refresh()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const state = session?.state
  const badgeClass = state === 'active' ? 'badge badge-active' : state && state.includes('blocked') || state === 'risk_stopped' ? 'badge badge-blocked' : 'badge'

  return (
    <Panel title="Session" error={error} loading={loading}>
      {session ? (
        <>
          <div className={badgeClass}>{STATE_LABEL[session.state]}</div>
          <dl className="kv">
            <dt>Trading active</dt>
            <dd>{session.trading_active ? 'Yes' : 'No'}</dd>
            <dt>Run time</dt>
            <dd>{duration(session.process_started_at)}</dd>
            <dt>Updated</dt>
            <dd>{time(session.updated_at)}</dd>
            {session.kill_switch_reason && (
              <>
                <dt>Kill switch reason</dt>
                <dd>{session.kill_switch_reason}</dd>
              </>
            )}
            {session.financial_integrity_reason && (
              <>
                <dt>Integrity block reason</dt>
                <dd>{session.financial_integrity_reason}</dd>
              </>
            )}
          </dl>

          <div className="button-row">
            <button disabled={busy} onClick={() => run(api.start)}>
              Start
            </button>
            <button disabled={busy} onClick={() => run(api.stop)}>
              Stop
            </button>
            <button disabled={busy || session.state !== 'risk_stopped'} onClick={() => run(api.resetRisk)}>
              Reset Risk
            </button>
            <button
              disabled={busy || session.state !== 'financial_integrity_blocked'}
              onClick={() => run(() => api.resetIntegrity(false))}
            >
              Reset Integrity (verified)
            </button>
          </div>

          {actionError && <div className="panel-error">{actionError}</div>}
        </>
      ) : null}
    </Panel>
  )
}
