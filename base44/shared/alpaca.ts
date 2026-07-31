// Shared Alpaca adapter — order placement, fill polling, and account state.
// Used by the canonical execution engine (shared/execution.ts) so that every
// trading surface settles accounting from CONFIRMED broker fills, never from
// requested quantity/price.

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

// Submit a market order. Returns the Alpaca order object (id, status, filled_qty, filled_avg_price...).
// client_order_id provides idempotency so a retried submit cannot duplicate a fill.
export async function placeAlpacaOrder({ apiKey, secretKey, mode, symbol, qty, side, client_order_id }) {
  const res = await fetch(`${baseUrl(mode)}/orders`, {
    method: 'POST',
    headers: { ...headers(apiKey, secretKey), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      symbol: String(symbol).toUpperCase(),
      qty: String(qty),
      side,
      type: 'market',
      time_in_force: 'day',
      client_order_id,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || data?.error || `Alpaca HTTP ${res.status}`);
  return data;
}

// Fetch the current state of an order — used to poll fills and detect rejections.
export async function getAlpacaOrder({ apiKey, secretKey, mode }, orderId) {
  const res = await fetch(`${baseUrl(mode)}/orders/${orderId}`, {
    headers: headers(apiKey, secretKey),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || `Alpaca HTTP ${res.status}`);
  return data;
}

// Account state — authoritative equity / buying power / cash for position sizing.
export async function getAlpacaAccount({ apiKey, secretKey, mode }) {
  const res = await fetch(`${baseUrl(mode)}/account`, {
    headers: headers(apiKey, secretKey),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || `Alpaca HTTP ${res.status}`);
  return data;
}