// Executable universe — the fixed list of highly liquid symbols the autonomous
// trader is allowed to execute. Candidates outside this universe are research-
// only: they can be analyzed and displayed, but never auto-executed.
//
// This narrows the failure surface for the proof stage: a small set of
// heavily-traded equities and ETFs with tight spreads, reliable quotes, and
// no exotic symbol routing issues.

export const EXECUTABLE_UNIVERSE = [
  // Mega-cap tech
  'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA',
  // Broad ETFs
  'SPY', 'QQQ', 'IWM', 'VTI', 'VOO', 'DIA',
  // Sector ETFs
  'XLF', 'XLE', 'XLK', 'XLV', 'XLI', 'XLU',
  // Blue chips
  'JPM', 'V', 'JNJ', 'WMT', 'PG', 'UNH', 'HD', 'MA', 'BAC', 'KO',
  // Bonds
  'TLT', 'IEF', 'SHY', 'BND',
  // Gold / commodities
  'GLD', 'SLV',
];

const UNIVERSE_SET = new Set(EXECUTABLE_UNIVERSE.map((s) => s.toUpperCase()));

// Check if a symbol is in the executable universe.
export function isExecutable(symbol) {
  return UNIVERSE_SET.has(String(symbol || '').toUpperCase());
}

// Filter a candidate list to only executable symbols.
export function filterExecutable(candidates) {
  return candidates.filter((c) => isExecutable(c.symbol));
}