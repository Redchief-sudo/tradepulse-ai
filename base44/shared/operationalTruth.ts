export function snapshotConservation(requested: number, captured: number, providerFailed: number, persistenceFailed: number) {
  const accounted = captured + providerFailed + persistenceFailed;
  return { requested, captured, provider_failed: providerFailed, persistence_failed: persistenceFailed, accounted, ok: requested === accounted && providerFailed === 0 && persistenceFailed === 0 };
}

export function healthStatus(issues: any[], inspectionErrors: any[]) {
  if (inspectionErrors.length) return 'health_unknown';
  if (issues.some((issue) => issue.severity === 'critical')) return 'unhealthy';
  if (issues.some((issue) => issue.severity === 'warning')) return 'degraded';
  return 'healthy';
}

export function dailyReportStatus(input: any) {
  if (input.financialIntegrityBlocked || input.settlementIntegrityBlocked > 0) return 'financial_integrity_blocked';
  if (input.reconciliationStatus !== 'clean' || input.unaccountedBrokerFills > 0 || input.missingLedgerFills > 0 || input.extraLedgerFills > 0 || input.positionDriftCount > 0) return 'reconciliation_required';
  if (input.brokerDataUnavailable || input.cashDataUnavailable || input.settlementPending > 0 || input.settlementFailed > 0 || input.unexplainedCandidates > 0 || input.healthCheckFailures > 0) return 'incomplete';
  return 'healthy';
}

export function brokerFillConservation(brokerFills: any[], ledgerFills: any[]) {
  const brokerIds = new Set(brokerFills.map((fill) => String(fill.id || fill.activity_id || '')).filter(Boolean));
  const brokerOrderIds = new Set(brokerFills.map((fill) => String(fill.order_id || '')).filter(Boolean));
  const ledgerIds = new Set(ledgerFills.map((fill) => String(fill.broker_fill_id || fill.fill_id || '')).filter(Boolean));
  const ledgerOrderIds = new Set(ledgerFills.map((fill) => String(fill.broker_order_id || '')).filter(Boolean));
  const brokerQtyByOrder = new Map<string, number>();
  const ledgerQtyByOrder = new Map<string, number>();
  for (const fill of brokerFills) if (fill.order_id) brokerQtyByOrder.set(String(fill.order_id), (brokerQtyByOrder.get(String(fill.order_id)) || 0) + Number(fill.qty || 0));
  for (const fill of ledgerFills) if (fill.broker_order_id) ledgerQtyByOrder.set(String(fill.broker_order_id), (ledgerQtyByOrder.get(String(fill.broker_order_id)) || 0) + Number(fill.filled_quantity || 0));
  const quantityMatches = (orderId: any) => orderId && Math.abs((brokerQtyByOrder.get(String(orderId)) || 0) - (ledgerQtyByOrder.get(String(orderId)) || 0)) < 1e-8;
  const missing = brokerFills.filter((fill) => !ledgerIds.has(String(fill.id || fill.activity_id || '')) && !quantityMatches(fill.order_id));
  const extra = ledgerFills.filter((fill) => !brokerIds.has(String(fill.broker_fill_id || fill.fill_id || '')) && !quantityMatches(fill.broker_order_id));
  return { alpaca_fill_count: brokerFills.length, ledger_fill_count: ledgerFills.length, missing_ledger_fills: missing.length, extra_ledger_fills: extra.length, missing_fill_ids: missing.map((fill) => fill.id || fill.activity_id), extra_fill_ids: extra.map((fill) => fill.broker_fill_id || fill.fill_id), ok: missing.length === 0 && extra.length === 0 };
}

export function reconciliationIsFresh(latestReconciliationAt: string | null, lastFillAt: string | null, marketCloseAt: string | null) {
  if (!latestReconciliationAt) return false;
  const reconciliationMs = new Date(latestReconciliationAt).getTime();
  return Number.isFinite(reconciliationMs)
    && (!lastFillAt || reconciliationMs >= new Date(lastFillAt).getTime())
    && (!marketCloseAt || reconciliationMs >= new Date(marketCloseAt).getTime());
}

export function isOpenBrokerOrder(intent: any) {
  const terminal = new Set(['filled', 'canceled', 'expired', 'rejected', 'replaced', 'done_for_day']);
  const remaining = Number(intent.unfilled_quantity ?? (Number(intent.requested_quantity || 0) - Number(intent.filled_quantity || 0)));
  return Boolean(intent.broker_order_id) && !terminal.has(String(intent.broker_terminal_status || intent.status || '').toLowerCase()) && remaining > 0;
}

export function settlementHealthSeverity(event: any) {
  if (['integrity_blocked', 'terminal_failed'].includes(event.status)) return 'critical';
  if (event.status === 'retryable_failed' && Number(event.retry_count || 0) >= Number(event.max_retries || 3)) return 'critical';
  return 'warning';
}
