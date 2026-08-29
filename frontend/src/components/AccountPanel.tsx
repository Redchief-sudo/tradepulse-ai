import { api } from '../api'
import { usePolling } from '../usePolling'
import { money } from '../format'
import { Panel } from './Panel'

export function AccountPanel() {
  const { data, error, loading } = usePolling(api.getAccount, 10000)
  return (
    <Panel title="Account" error={error} loading={loading}>
      {data ? (
        <dl className="kv">
          <dt>Equity</dt>
          <dd>{money(data.equity)}</dd>
          <dt>Cash</dt>
          <dd>{money(data.cash)}</dd>
          <dt>Buying power</dt>
          <dd>{money(data.buying_power)}</dd>
          <dt>Last equity (prior close)</dt>
          <dd>{money(data.last_equity)}</dd>
        </dl>
      ) : null}
    </Panel>
  )
}
