import { useState } from 'react'
import { api, ApiError } from '../api'
import { usePolling } from '../usePolling'
import { time } from '../format'
import { Panel } from './Panel'
import type { SessionState } from '../types'

const CONFIRMATION_PHRASE = 'RESET_FINANCIAL_INTEGRITY'

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
  const [confirmText, setConfirmText] = useState('')
  const [showForceConfirm, setShowForceConfirm] = useState(false)

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

          {session.state === 'financial_integrity_blocked' && (
            <div className="danger-zone">
              {!showForceConfirm ? (
                <button className="danger" disabled={busy} onClick={() => setShowForceConfirm(true)}>
                  Force Reset Integrity (skip verification)
                </button>
              ) : (
                <div className="confirm-box">
                  <p>
                    This skips the verifying reconciliation pass and is logged as a{' '}
                    <strong>critical, unverified</strong> action. Type <code>{CONFIRMATION_PHRASE}</code> to confirm.
                  </p>
                  <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} placeholder={CONFIRMATION_PHRASE} />
                  <div className="button-row">
                    <button
                      className="danger"
                      disabled={busy || confirmText !== CONFIRMATION_PHRASE}
                      onClick={() =>
                        run(() => api.resetIntegrity(true, confirmText)).then(() => {
                          setShowForceConfirm(false)
                          setConfirmText('')
                        })
                      }
                    >
                      Confirm Force Reset
                    </button>
                    <button
                      onClick={() => {
                        setShowForceConfirm(false)
                        setConfirmText('')
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {actionError && <div className="panel-error">{actionError}</div>}
        </>
      ) : null}
    </Panel>
  )
}
