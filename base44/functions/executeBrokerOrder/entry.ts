import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { placeAlpacaOrder } from '../../shared/alpaca.ts';

// Places a real market order via the connected broker using the USER's stored
// credentials (broker_api_key / broker_api_secret on their profile). Respects
// broker_mode (paper vs live). Currently supports Alpaca.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const { symbol, qty, side } = body;
    if (!symbol || !qty || !side) {
      return Response.json({ error: 'symbol, qty, and side are required' }, { status: 400 });
    }
    if (user.broker !== 'alpaca') {
      return Response.json({ error: 'Live execution currently supports Alpaca only' }, { status: 400 });
    }
    if (!user.broker_api_key || !user.broker_api_secret) {
      return Response.json({ error: 'Broker not connected — add API keys in Settings' }, { status: 400 });
    }

    const order = await placeAlpacaOrder({
      apiKey: user.broker_api_key,
      secretKey: user.broker_api_secret,
      mode: user.broker_mode || 'paper',
      symbol,
      qty,
      side,
    });
    return Response.json({ ok: true, order });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}