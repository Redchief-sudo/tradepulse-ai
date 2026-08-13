export function summarizeCandidateDispositions(candidates: any[], dispositions: Map<string, string>) {
  const symbols = candidates.map((candidate) => String(candidate.symbol).toUpperCase());
  const missing = symbols.filter((symbol) => !dispositions.has(symbol));
  const counts: Record<string, number> = {};
  for (const symbol of symbols) {
    const disposition = dispositions.get(symbol);
    if (!disposition) continue;
    counts[disposition] = (counts[disposition] || 0) + 1;
  }
  return {
    ok: missing.length === 0 && Object.values(counts).reduce((sum, count) => sum + count, 0) === symbols.length,
    total: symbols.length,
    accounted: symbols.length - missing.length,
    missing,
    counts,
    dispositions: Object.fromEntries(symbols.filter((symbol) => dispositions.has(symbol)).map((symbol) => [symbol, dispositions.get(symbol)])),
  };
}
