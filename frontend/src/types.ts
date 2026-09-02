// Loosely-typed mirrors of the JSON the FastAPI backend returns (see
// tradepulse/web/app.py) -- every value that started as a Decimal on the
// Python side arrives as a string here (TradePulse's own JSON-safe
// encoder never sends a raw float for money), so amounts are typed
// `string` throughout and parsed with Number()/parseFloat() only at the
// point of display.

export type SessionState =
  | 'disabled'
  | 'active'
  | 'risk_stopped'
  | 'system_degraded'
  | 'broker_unavailable'
  | 'market_closed'
  | 'manually_stopped'
  | 'financial_integrity_blocked'

export interface TradingSession {
  session_id: string
  state: SessionState
  trading_active: boolean
  updated_at: string
  kill_switch_reason: string | null
  kill_switch_at: string | null
  kill_switch_reset_required: boolean
  financial_integrity_reason: string | null
  financial_integrity_manual_reenable_required: boolean
}

export interface SessionActionResult {
  exit_code: number
  session: TradingSession
}

export interface AlpacaAccount {
  equity: string
  last_equity: string
  cash: string
  buying_power: string
  portfolio_value: string
}

export interface AlpacaPosition {
  symbol: string
  asset_class: string
  qty: string
  avg_entry_price: string
  market_value: string
  current_price: string
  unrealized_pl: string
}

export interface EnrichedPosition {
  position: AlpacaPosition
  stop_loss: string | null
  target_price: string | null
}

export interface ScanRunCapability {
  scan_run_id: string
  asset_class: string
  market_data_tier: string | null
  equity_feed: string | null
  option_feed: string | null
  completed_at: string | null
}

export type MarketDataCapabilityByLane = Record<string, ScanRunCapability>

export interface AssetIdentity {
  symbol: string
  asset_class: string
  native_asset_id: string
  venue: string | null
  metadata: Record<string, unknown>
}

export interface Opportunity {
  opportunity_id: string
  scan_generation: string
  asset: AssetIdentity
  quote: { price: string; provider: string; observed_at: string }
  source: string
  created_at: string
  confidence: number | null
  metadata: Record<string, string | null>
}

export interface TradeIntent {
  trade_intent_id: string
  asset: AssetIdentity
  side: 'buy' | 'sell'
  strategy: string
  created_at: string
  requested_quantity: string | null
  status: string
  filled_quantity: string
  filled_avg_price: string | null
  rejection_reason: string | null
  risk_snapshot: Record<string, unknown>
}

export interface Fill {
  fill_id: string
  trade_intent_id: string
  asset: AssetIdentity
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fees: string
  filled_at: string
}

export interface SettlementEvent {
  settlement_event_id: string
  fill_id: string
  asset: AssetIdentity
  side: 'buy' | 'sell'
  quantity: string
  price: string
  status: string
  occurred_at: string
  realized_pnl: string | null
  error_code: string | null
}

export interface PnlRecord {
  record_id: string
  asset: AssetIdentity
  realized: string
  unrealized: string
  as_of: string
}

export interface PnlResponse {
  realized: PnlRecord[]
  unrealized_total: string
  positions_unrealized: AlpacaPosition[]
}

export interface PortfolioSnapshot {
  snapshot_id: string
  as_of: string
  total_equity: string
  cash_balance: string
  holdings_value: string
  sector_exposure: Record<string, string>
  open_positions: number
  outstanding_orders: number
  trades_today: number
  daily_pnl_pct: string
  source: string
}

export interface ReconciliationRecord {
  record_id: string
  reconciliation_type: string
  subject_id: string
  outcome: 'matched' | 'drift_detected' | 'corrected'
  occurred_at: string
  corrective_action: string | null
}

export interface AuditEvent {
  event_id: string
  event_type: string
  severity: 'info' | 'warning' | 'error' | 'critical'
  message: string
  occurred_at: string
  entity_type: string | null
  entity_id: string | null
}

export interface ScanRun {
  scan_run_id: string
  scan_generation: string
  trigger: string
  asset_class: string
  status: 'running' | 'completed' | 'failed'
  started_at: string
  completed_at: string | null
  // The CONFIGURED universe offered to the AI this cycle -- distinct from
  // candidates_discovered (what the AI returned) and candidates_approved
  // (what cleared the full gate chain). 0 on a legacy row predating this field.
  universe_size: number
  candidates_discovered: number
  candidates_approved: number
  orders_submitted: number
  error: string | null
  market_data_tier: string | null
  equity_feed: string | null
  option_feed: string | null
  // Links to the AIResponse row holding this cycle's raw candidate list
  // (see api.getAiResponse) -- null when no AI response was ever obtained
  // for this run (e.g. blocked before the AI call, or a legacy row).
  ai_response_request_id: string | null
  // Market Regime Phase 2 -- one broad-market benchmark classification per
  // lane per cycle (SPY for equity/options, BTC/USD for crypto). `regime`
  // is one of the 5 classified labels, or the literal "unavailable" when
  // the benchmark fetch/classification failed (see `regime_reason`, only
  // ever set alongside "unavailable"). All null on a legacy row predating
  // this field, or before the classifier ran for this cycle.
  regime: string | null
  regime_reason: string | null
  regime_confidence: number | null
  regime_position_multiplier: string | null
  regime_realized_vol: string | null
}

export interface AiCandidate {
  symbol: string
  recommendation: string
  confidence: number
  summary: string
}

export interface AiResponse {
  request_id: string
  provider: string
  model: string
  completed_at: string
  result: { candidates: AiCandidate[] }
  latency_ms: number
}
