export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const n = typeof value === 'string' ? Number(value) : value
  if (Number.isNaN(n)) return '—'
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

export function pct(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const n = typeof value === 'string' ? Number(value) : value
  if (Number.isNaN(n)) return '—'
  return `${n.toFixed(2)}%`
}

export function time(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

export interface OccContract {
  root: string
  expiry: string // YYYY-MM-DD
  right: 'C' | 'P'
  strike: number
}

/** Deterministic OCC option-symbol decode -- DISPLAY-ONLY. The backend's
 * asset identity (symbol/native_asset_id, and the separately-recorded
 * underlying_symbol/expiry/strike/option_type metadata on Opportunity/
 * TradeIntent/Fill/SettlementEvent) remains the sole authority; this never
 * becomes an identity/reconciliation/lifecycle-join/risk/accounting
 * authority of its own. Returns null (never a guessed/partial contract) on
 * anything that doesn't match the standard 21-char OCC layout -- callers
 * must fall back to rendering the raw symbol string. */
export function parseOccSymbol(symbol: string | null | undefined): OccContract | null {
  if (!symbol) return null
  const match = /^([A-Z]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(symbol.trim())
  if (!match) return null
  const [, root, yy, mm, dd, right, strikeStr] = match
  const month = Number(mm)
  const day = Number(dd)
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  const strike = Number(strikeStr) / 1000
  return { root, expiry: `20${yy}-${mm}-${dd}`, right: right as 'C' | 'P', strike }
}

export function relativeAgo(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value).getTime()
  if (Number.isNaN(d)) return value
  const seconds = Math.max(0, Math.floor((Date.now() - d) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
