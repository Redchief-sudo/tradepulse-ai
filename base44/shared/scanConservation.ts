export function summarizeCandidateDispositions(candidates: any[], dispositions: Map<string, string>) {
  const entries = candidates.map((candidate, index) => ({ candidate_id: candidate.candidate_id || candidate.id || `${String(candidate.symbol).toUpperCase()}#${index + 1}`, symbol: String(candidate.symbol).toUpperCase(), disposition: dispositions.get(String(candidate.symbol).toUpperCase()) }));
  const missing = entries.filter((entry) => !entry.disposition).map((entry) => entry.candidate_id);
  const counts: Record<string, number> = {};
  for (const { disposition } of entries) {
    if (!disposition) continue;
    counts[disposition] = (counts[disposition] || 0) + 1;
  }
  return {
    ok: missing.length === 0 && Object.values(counts).reduce((sum, count) => sum + count, 0) === entries.length,
    total: entries.length,
    accounted: entries.length - missing.length,
    missing,
    counts,
    dispositions: entries.filter((entry) => entry.disposition),
  };
}

export function summarizeFillSettlement(fills: any[], events: any[]) {
  const byFillId = new Map();
  for (const event of events) {
    for (const key of [event.event_id, event.broker_fill_id].filter(Boolean)) byFillId.set(key, event);
  }
  const summary = { accepted_fills: fills.length, settlement_completed: 0, settlement_pending: 0, settlement_failed: 0, settlement_integrity_blocked: 0, unaccounted_fills: 0 };
  for (const fill of fills) {
    const event = byFillId.get(fill.fill_id) || byFillId.get(fill.broker_fill_id);
    if (!event) { summary.unaccounted_fills++; continue; }
    if (event.status === 'completed' && event.integrity_verified === true) summary.settlement_completed++;
    else if (event.status === 'integrity_blocked') summary.settlement_integrity_blocked++;
    else if (event.status === 'terminal_failed') summary.settlement_failed++;
    else summary.settlement_pending++;
  }
  return { ...summary, ok: fills.length === summary.settlement_completed && summary.unaccounted_fills === 0 };
}
