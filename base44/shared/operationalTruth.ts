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
  if (input.reconciliationStatus !== 'clean' || input.unaccountedBrokerFills > 0 || input.positionDriftCount > 0) return 'reconciliation_required';
  if (input.brokerDataUnavailable || input.cashDataUnavailable || input.settlementPending > 0 || input.settlementFailed > 0 || input.unexplainedCandidates > 0 || input.healthCheckFailures > 0) return 'incomplete';
  return 'healthy';
}
