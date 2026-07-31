// Shared Alpaca order placement — used by both the user-auth executeBrokerOrder
// function (manual trades) and the admin/scheduled runStopLossCycle (autonomous).
const PAPER_URL = 'https://paper-api.alpaca.markets/v2/orders';
const LIVE_URL = 'https://api.alpaca.markets/v2/orders';

export async function placeAlpacaOrder({ apiKey, secretKey, mode, symbol, qty, side }) {
  const url = mode === 'live' ? LIVE_URL : PAPER_URL;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'APCA-API-KEY-ID': apiKey,
      'APCA-API-SECRET-KEY': secretKey,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      symbol: String(symbol).toUpperCase(),
      qty: String(qty),
      side,
      type: 'market',
      time_in_force: 'day',
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || data?.error || `Alpaca HTTP ${res.status}`);
  return data;
}