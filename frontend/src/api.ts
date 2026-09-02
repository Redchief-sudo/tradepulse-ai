import type {
  AiResponse,
  AlpacaAccount,
  AuditEvent,
  EnrichedPosition,
  Fill,
  MarketDataCapabilityByLane,
  Opportunity,
  PnlResponse,
  PortfolioSnapshot,
  ReconciliationRecord,
  ScanRun,
  SessionActionResult,
  SettlementEvent,
  TradeIntent,
  TradingSession,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.detail ?? `${path} failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new ApiError(response.status, payload.detail ?? `${path} failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const api = {
  getSession: () => get<TradingSession>('/api/session'),
  start: () => post<SessionActionResult>('/api/session/start'),
  stop: () => post<SessionActionResult>('/api/session/stop'),
  resetRisk: () => post<SessionActionResult>('/api/session/reset-risk'),
  resetIntegrity: (force: boolean, confirmation?: string) =>
    post<SessionActionResult>('/api/session/reset-integrity', { force, confirmation }),

  getAccount: () => get<AlpacaAccount>('/api/account'),
  getPositions: () => get<EnrichedPosition[]>('/api/positions'),
  getRiskExposure: () => get<PortfolioSnapshot>('/api/risk-exposure'),
  getPnl: () => get<PnlResponse>('/api/pnl'),

  getMarketDataCapability: () => get<MarketDataCapabilityByLane>('/api/market-data-capability'),
  probeMarketDataCapability: () =>
    post<{ tier_label: string; equity_feed: string; option_feed: string }>('/api/market-data-capability/probe'),

  getOpportunities: (limit = 50) => get<Opportunity[]>(`/api/opportunities?limit=${limit}`),
  getTradeIntents: (status?: string, limit = 50) =>
    get<TradeIntent[]>(`/api/trade-intents?limit=${limit}${status ? `&status=${status}` : ''}`),
  getFills: (limit = 50) => get<Fill[]>(`/api/fills?limit=${limit}`),
  getSettlements: (limit = 50) => get<SettlementEvent[]>(`/api/settlements?limit=${limit}`),
  getReconciliation: (limit = 50) => get<ReconciliationRecord[]>(`/api/reconciliation?limit=${limit}`),
  getAuditEvents: (limit = 50) => get<AuditEvent[]>(`/api/audit-events?limit=${limit}`),
  getScanRuns: (limit = 20) => get<ScanRun[]>(`/api/scan-runs?limit=${limit}`),
  getAiResponse: (requestId: string) => get<AiResponse | null>(`/api/ai-responses/${requestId}`),
}
