import { api } from '../api'
import { usePolling } from '../usePolling'

// Provenance never changes during a running process -- fetched once (a
// long poll interval, not a new one-shot hook) and shown as a restrained
// attribution line, never cluttering the trading panels above it.
const PROVENANCE_POLL_MS = 24 * 60 * 60 * 1000

export function Footer() {
  const { data } = usePolling(api.getProvenance, PROVENANCE_POLL_MS)
  if (!data) return null

  const shortFingerprint = data.build_fingerprint.slice(0, 12)

  return (
    <footer className="footer">
      <span>
        TradePulse • Created by {data.creator_name}
      </span>
      <span>
        © {data.copyright_years} {data.copyright_owner} • {data.company_name}
      </span>
      <span title={`v${data.software_version} • commit ${data.git_commit} • fingerprint ${data.build_fingerprint}`}>
        Build: {shortFingerprint}
      </span>
    </footer>
  )
}
