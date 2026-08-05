// Shared Alpaca adapter — order placement, fill polling, account state, and activities.
// Used by the canonical execution engine and reconciliation.
import { throwAlpacaError, extractRequestId } from './alpacaErrors.ts';

const PAPER_BASE = 'https://paper-api.alpaca.markets/v2';
const LIVE_BASE = 'https://api.alpaca.markets/v2';

function baseUrl(mode) {
  return mode === 'live' ? LIVE_BASE : PAPER_BASE;
}

function headers(apiKey, secretKey) {
  return {
    'APCA-API-KEY-ID': apiKey,
    'APCA-API-SECRET-KEY': secretKey,
  };
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

// Account state — authoritative equity / buying power / cash for position sizing.
export async function getAlpacaAccount({ apiKey, secretKey, mode }) {
  const res = await fetch(`${baseUrl(mode)}/account`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getAccount');
  const data = await res.json().catch(() => ({}));
  return data;
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
export async function getAlpacaActivities({ apiKey, secretKey, mode, activityType = 'FILL', sinceDate }) {
  const params = new URLSearchParams({ activity_types: activityType });
  if (sinceDate) params.set('after', sinceDate);
  const res = await fetch(`${baseUrl(mode)}/account/activities?${params.toString()}`, {
    headers: headers(apiKey, secretKey),
  });
  if (!res.ok) await throwAlpacaError(res, 'getActivities');
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data) ? data : [];
}