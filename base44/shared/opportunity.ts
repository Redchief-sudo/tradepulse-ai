import { isExecutable } from './executableUniverse.ts';

export const EXECUTION_CAPABILITIES = ['research_only', 'paper_executable', 'live_executable', 'unsupported'] as const;

export function normalizeAssetClass(value: unknown) {
  const normalized = String(value || '').toLowerCase();
  if (normalized === 'stocks' || normalized === 'stock' || normalized === 'equity') return 'equities';
  if (normalized === 'commodity' || normalized === 'futures') return 'commodities';
  return ['equities', 'crypto', 'forex', 'commodities', 'fixed_income'].includes(normalized)
    ? normalized : 'unsupported';
}

export function marketSession(assetClass: string, providerTimestamp: string | null) {
  if (!providerTimestamp) return 'unknown';
  if (assetClass === 'crypto') return 'continuous';
  const date = new Date(providerTimestamp);
  if (!Number.isFinite(date.getTime())) return 'unknown';
  const weekday = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(date);
  if (weekday === 'Sat' || weekday === 'Sun') return 'closed';
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date);
  const hour = Number(parts.find((part) => part.type === 'hour')?.value);
  const minute = Number(parts.find((part) => part.type === 'minute')?.value);
  const minutes = hour * 60 + minute;
  if (minutes < 570) return 'pre_market';
  if (minutes < 960) return 'regular';
  return 'after_hours';
}

export function canonicalizeOpportunity(candidate: any, quote: any, receivedAt: string, brokerMode = 'paper', executableAssetClasses: string[] = []) {
  const assetClass = normalizeAssetClass(candidate.asset_class);
  const nativeSymbol = String(candidate.native_symbol || candidate.symbol || '').trim().toUpperCase();
  const providerTimestamp = quote?.quote_timestamp
    ? new Date(Number(quote.quote_timestamp) * 1000).toISOString() : null;
  const observationAgeMs = providerTimestamp
    ? new Date(receivedAt).getTime() - new Date(providerTimestamp).getTime() : Number.POSITIVE_INFINITY;
  const freshnessLimitMs = assetClass === 'crypto' ? 2 * 60_000 : 15 * 60_000;
  const fresh = observationAgeMs >= 0 && observationAgeMs <= freshnessLimitMs;
  const hasAuthoritativeQuote = !quote?.error && Number(quote?.price) > 0 && Boolean(providerTimestamp) && fresh;
  const equityLikeEtf = (assetClass === 'commodities' || assetClass === 'fixed_income') && !nativeSymbol.includes('=') && isExecutable(nativeSymbol);
  const explicitlyExecutable = (assetClass === 'crypto' && executableAssetClasses.includes('crypto'))
    || ((assetClass === 'equities' || equityLikeEtf) && isExecutable(nativeSymbol));
  let executionCapability = 'unsupported';
  if (hasAuthoritativeQuote) {
    executionCapability = explicitlyExecutable
      ? (brokerMode === 'live' ? 'live_executable' : 'paper_executable')
      : 'research_only';
  }
  const bid = Number(quote?.bid) || null;
  const ask = Number(quote?.ask) || null;
  return {
    asset_class: assetClass,
    native_symbol: nativeSymbol,
    canonical_symbol: nativeSymbol.replace(/[^A-Z0-9]/g, ''),
    venue: quote?.venue || null,
    market: quote?.market || null,
    price: hasAuthoritativeQuote ? Number(quote.price) : null,
    bid,
    ask,
    spread: bid && ask && ask >= bid ? ask - bid : null,
    volume: Number(quote?.volume) || null,
    liquidity: null,
    volatility: null,
    provider_timestamp: providerTimestamp,
    received_at: receivedAt,
    market_session: marketSession(assetClass, providerTimestamp),
    source: quote?.source || null,
    execution_capability: executionCapability,
    provider_error: quote?.error || (!fresh && providerTimestamp ? 'stale provider timestamp' : null),
  };
}
