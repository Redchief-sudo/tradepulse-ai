export const SETTLEMENT_STAGES = [
  ['lot_projected', 'projectLot'],
  ['cash_projected', 'projectCash'],
  ['holding_projected', 'projectHolding'],
  ['trade_projected', 'projectTrade'],
  ['decision_projected', 'projectDecision'],
  ['intent_projected', 'projectIntent'],
  ['integrity_verified', 'verifyIntegrity'],
] as const;

export const MAX_SETTLEMENT_ATTEMPTS = 8;
export const RETRY_BASE_MS = 15_000;
export const RETRY_MAX_MS = 5 * 60_000;

export function selectLeaseWinner(locks: any[]) {
  return [...locks].sort((left, right) => {
    const timeDifference = new Date(left.acquired_at).getTime() - new Date(right.acquired_at).getTime();
    if (timeDifference !== 0) return timeDifference;
    return String(left.id).localeCompare(String(right.id));
  })[0] || null;
}

export function deriveOrderSettlementSummary(intent: any, fills: any[]) {
  const requestedQuantity = Number(intent?.requested_quantity) || 0;
  const cumulativeQuantity = fills.reduce((sum, fill) => sum + (Number(fill.filled_quantity) || 0), 0);
  const brokerTerminal = Boolean(intent?.broker_terminal_status);
  const intentTerminal = ['filled', 'canceled', 'rejected', 'expired', 'failed'].includes(intent?.status);
  const orderTerminal = brokerTerminal || intentTerminal;
  const fullyFilled = requestedQuantity > 0 && cumulativeQuantity >= requestedQuantity - 0.0001;
  const orderStatus = orderTerminal
    ? (fullyFilled ? 'filled' : intent.broker_terminal_status || intent.status)
    : (cumulativeQuantity > 0 ? 'partially_filled' : intent?.status || 'accepted');
  const timestamps = fills
    .map((fill) => fill.timestamp)
    .filter(Boolean)
    .sort((left, right) => new Date(left).getTime() - new Date(right).getTime());
  return {
    orderStatus,
    settlementState: orderTerminal ? 'settled' : 'current_fills_settled',
    firstFillAt: timestamps[0] || null,
    lastFillAt: timestamps[timestamps.length - 1] || null,
  };
}

export function retryDelayMs(attempt: number) {
  return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.max(0, attempt - 1));
}

export function isSettlementProcessable(event: any, nowMs: number, staleLeaseMs: number) {
  if (event.status === 'pending') return true;
  if (event.status === 'retryable_failed') {
    return !event.next_retry_at || new Date(event.next_retry_at).getTime() <= nowMs;
  }
  return event.status === 'processing' && event.processing_started_at &&
    nowMs - new Date(event.processing_started_at).getTime() > staleLeaseMs;
}

export function classifySettlementFailure(event: any, error: Error, nowMs = Date.now()) {
  const attempts = Number(event.attempt_count || 0) + 1;
  const integrityBlocked = error.message.startsWith('INTEGRITY_VIOLATION');
  const exhausted = attempts >= MAX_SETTLEMENT_ATTEMPTS;
  const status = integrityBlocked ? 'integrity_blocked' : exhausted ? 'terminal_failed' : 'retryable_failed';
  return {
    status,
    attempt_count: attempts,
    failure_kind: integrityBlocked ? 'integrity' : exhausted ? 'retry_exhausted' : 'transient_projection',
    error: error.message,
    next_retry_at: status === 'retryable_failed' ? new Date(nowMs + retryDelayMs(attempts)).toISOString() : null,
    processing_owner: null,
    processing_started_at: null,
  };
}

export async function runSettlementStages(event: any, handlers: Record<string, Function>, checkpoint: Function, beforeStage?: Function) {
  let state = { ...event };
  for (const [flag, handlerName] of SETTLEMENT_STAGES) {
    if (state[flag]) continue;
    if (beforeStage) await beforeStage(flag, state);
    const result = await handlers[handlerName](state);
    const patch = { [flag]: true, ...(result?.patch || {}) };
    await checkpoint(patch);
    state = { ...state, ...patch };
  }
  return state;
}
