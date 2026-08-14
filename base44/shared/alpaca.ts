// Shared Alpaca adapter — order placement, fill polling, account state, and activities.
// Used by the canonical execution engine and reconciliation.
import { throwAlpacaError, extractRequestId } from './alpacaErrors.ts';

const PAPER_BASE = 'https://paper-api.alpaca.markets/v2';
const LIVE_BASE = 'https://api.alpaca.markets/v2';
const DATA_BASE = 'https://data.alpaca.markets';

function baseUrl(mode) {
  return mode === 'live' ? LIVE_BASE : PAPER_BASE;
}

function headers(apiKey, secretKey) {
  return {
    'APCA-API-KEY-ID': apiKey,
    'APCA-API-SECRET-KEY': secretKey,
  };
}

export function normalizeAlpacaActivitySide(side) {
  const normalized = String(side || '').toLowerCase();
  if (normalized === 'buy' || normalized === 'buy_to_cover') return 'buy';
  if (normalized === 'sell' || normalized === 'sell_short') return 'sell';
  return null;
}

const CRYPTO_QUOTE_CURRENCIES = ['USDT', 'USDC', 'USD', 'BTC'];

// Alpaca uses different pair renderings across market-data, order, activity,
// and position responses (for example BTC/USD and BTCUSD). Keep one app-side
// identity so fills and broker positions reconcile to the originating intent.
export function normalizeAlpacaSymbol(symbol, assetClass = 'stocks') {
  const raw = String(symbol || '').trim().toUpperCase().replace(/-/g, '/');
  if (String(assetClass || '').toLowerCase() !== 'crypto' || raw.includes('/')) return raw;
  for (const quote of CRYPTO_QUOTE_CURRENCIES) {
    if (raw.length > quote.length && raw.endsWith(quote)) {
      return `${raw.slice(0, -quote.length)}/${quote}`;
    }
  }
  return raw;
}

export function inferAlpacaAssetClass(symbol, declaredAssetClass = null) {
  if (String(declaredAssetClass || '').toLowerCase() === 'crypto') return 'crypto';
  const raw = String(symbol || '').toUpperCase();
  if (raw.includes('/') || raw.includes('-')) return 'crypto';
  return /^(BTC|ETH|SOL|DOGE|AVAX|LINK|LTC|BCH|UNI|AAVE|DOT|SHIB)(USD|USDT|USDC|BTC)$/.test(raw)
    ? 'crypto'
    : 'stocks';
}

// Submit an order. Respects order_type (market/limit/stop/stop_limit), limit_price,
// stop_price, and time_in_force from the TradeIntent — no longer hardcodes market/day.
// client_order_id provides idempotency so a retried submit cannot duplicate a fill.
export async function placeAlpacaOrder({ apiKey, secretKey, mode, symbol, qty, side, client_order_id, order_type = 'market', limit_price, stop_price, time_in_force = 'day' }) {
  const body = {
    symbol: String(symbol).toUpperCase(),
    qty: String(qty),
    side,
    type: order_type,
    time_in_force,
    client_order_id,
  };
  if ((order_type === 'limit' || order_type === 'stop_limit') && limit_price != null) {
    body.limit_price = String(limit_price);
  }
  if ((order_type === 'stop' || order_type === 'stop_limit') && stop_price != null) {
    body.stop_price = String(stop_price);
  }
  const res = await fetch(`${baseUrl(mode)}/orders`, {
    method: 'POST',
    headers: { ...headers(apiKey, secretKey), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) await throwAlpacaError(res, 'placeOrder');
  const data = await res.json().catch(() => ({}));
  data._request_id = extractRequestId(res);
  return data;
}

// Fetch the current state of an order — used to poll fills and detect rejections.
export async function getAlpacaOrder({ apiKey, secretKey, mode }, orderId) {
  const res = await fetch(`${baseUrl(mode)}/orders/${orderId}`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getOrder');
  const data = await res.json().catch(() => ({}));
  return data;
}

// Recent broker orders, including pending, partially filled, filled, canceled,
// and rejected orders. This prevents submitted orders from disappearing while
// they are waiting for their first fill activity.
export async function getAlpacaOrders({ apiKey, secretKey, mode }, { limit = 10 } = {}) {
  const params = new URLSearchParams({
    status: 'all',
    limit: String(limit),
    direction: 'desc',
    nested: 'true',
  });
  const res = await fetch(`${baseUrl(mode)}/orders?${params.toString()}`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getOrders');
  const data = await res.json().catch(() => []);
  return Array.isArray(data) ? data : [];
}

// Account state — authoritative equity / buying power / cash for position sizing.
export async function getAlpacaAccount({ apiKey, secretKey, mode }) {
  const res = await fetch(`${baseUrl(mode)}/account`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getAccount');
  const data = await res.json().catch(() => ({}));
  return data;
}

// Current broker positions — authoritative for portfolio display and
// reconciliation. This is a read-only snapshot; settlement remains the sole
// owner of financial-ledger and Holding projection writes.
export async function getAlpacaPositions({ apiKey, secretKey, mode }) {
  const res = await fetch(`${baseUrl(mode)}/positions`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getPositions');
  const data = await res.json().catch(() => []);
  return Array.isArray(data) ? data : [];
}

// Get Alpaca market clock — authoritative market session state.
// Returns { is_open, next_open, next_close, timestamp, session }.
// This is the authoritative source for whether the US regular session is open,
// rather than relying on local time or cron schedules.
export async function getAlpacaClock({ apiKey, secretKey, mode }) {
  const res = await fetch(`${baseUrl(mode)}/clock`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getClock');
  const data = await res.json().catch(() => ({}));
  return data;
}

// Latest executable top-of-book quote from Alpaca Market Data. Unlike a last
// trade price, bid/ask is sufficient to enforce spread and estimated-slippage
// limits before an order reaches the broker.
export async function getAlpacaLatestQuote({ apiKey, secretKey }, symbol, assetClass = 'stocks') {
  const isCrypto = String(assetClass).toLowerCase() === 'crypto';
  const normalized = String(symbol).toUpperCase().replace(/-/g, '/');
  const url = isCrypto
    ? `${DATA_BASE}/v1beta3/crypto/us/latest/quotes?symbols=${encodeURIComponent(normalized)}`
    : `${DATA_BASE}/v2/stocks/${encodeURIComponent(normalized)}/quotes/latest?feed=iex`;
  const res = await fetch(url, { headers: headers(apiKey, secretKey) });
  if (!res.ok) await throwAlpacaError(res, 'getLatestQuote');
  const data = await res.json().catch(() => ({}));
  const quote = isCrypto ? data?.quotes?.[normalized] : data?.quote;
  const bid = Number(quote?.bp);
  const ask = Number(quote?.ap);
  const timestamp = quote?.t || null;
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || ask < bid) {
    throw new Error(`ALPACA_QUOTE_INVALID: ${normalized}`);
  }
  return { bid, ask, timestamp, symbol: normalized, source: isCrypto ? 'alpaca_crypto' : 'alpaca_iex' };
}

export async function getAlpacaCryptoBars({ apiKey, secretKey }, symbol, from, to) {
  const normalized = String(symbol).toUpperCase().replace(/-/g, '/');
  const params = new URLSearchParams({
    symbols: normalized,
    timeframe: '1Day',
    start: new Date(from * 1000).toISOString(),
    end: new Date(to * 1000).toISOString(),
    limit: '1000',
    sort: 'asc',
  });
  const res = await fetch(`${DATA_BASE}/v1beta3/crypto/us/bars?${params.toString()}`, { headers: headers(apiKey, secretKey) });
  if (!res.ok) await throwAlpacaError(res, 'getCryptoBars');
  const data = await res.json().catch(() => ({}));
  const rows = data?.bars?.[normalized];
  if (!Array.isArray(rows) || rows.length < 30) throw new Error(`ALPACA_CRYPTO_BARS_INSUFFICIENT: ${normalized} returned ${Array.isArray(rows) ? rows.length : 0}`);
  return rows.map((bar) => ({
    date: new Date(bar.t).toISOString().slice(0, 10),
    open: Number(bar.o), high: Number(bar.h), low: Number(bar.l), close: Number(bar.c), volume: Number(bar.v) || 0,
  })).filter((bar) => Number.isFinite(bar.close) && bar.close > 0);
}

// Cancel an open order — used by the kill switch to cancel unfilled entry orders.
export async function cancelAlpacaOrder({ apiKey, secretKey, mode }, orderId) {
  const res = await fetch(`${baseUrl(mode)}/orders/${orderId}`, {
    method: 'DELETE',
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'cancelOrder');
  return { canceled: true, orderId };
}

// Fetch account activities (trade fills) — used by reconciliation to find the ACTUAL
// close price of a position that was externally closed, rather than inventing one.
// Returns an array of activity objects with { activity_type, symbol, qty, price, side, date }
export async function getAlpacaActivities({
  apiKey,
  secretKey,
  mode,
  activityType = 'FILL',
  sinceDate,
  pageSize,
  direction,
  paginate = false,
}) {
  const activities = [];
  let pageToken = null;
  do {
    const params = new URLSearchParams({ activity_types: activityType });
    if (sinceDate) params.set('after', sinceDate);
    if (pageSize) params.set('page_size', String(pageSize));
    if (direction) params.set('direction', direction);
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`${baseUrl(mode)}/account/activities?${params.toString()}`, { headers: headers(apiKey, secretKey) });
    if (!res.ok) await throwAlpacaError(res, 'getActivities');
    const data = await res.json().catch(() => []);
    const page = Array.isArray(data) ? data : [];
    activities.push(...page);
    pageToken = paginate && pageSize && page.length === Number(pageSize) ? String(page[page.length - 1]?.id || page[page.length - 1]?.activity_id || '') : null;
  } while (pageToken);
  return activities;
}
