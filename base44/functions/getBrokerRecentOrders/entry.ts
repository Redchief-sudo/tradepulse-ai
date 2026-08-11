import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaOrders } from '../../shared/alpaca.ts';

function normalizeOrder(order) {
  const requestedQuantity = Number(order.qty);
  const filledQuantity = Number(order.filled_qty);
  const filledPrice = Number(order.filled_avg_price);
  return {
    id: order.id,
    broker_order_id: order.id,
    client_order_id: order.client_order_id || null,
    symbol: String(order.symbol || '').toUpperCase(),
    action: String(order.side || '').toLowerCase(),
    shares: requestedQuantity,
    filled_qty: filledQuantity,
    price: Number.isFinite(filledPrice) ? filledPrice : null,
    total_value: Number.isFinite(filledPrice) ? filledQuantity * filledPrice : null,
    status: order.status || 'unknown',
    submitted_at: order.submitted_at || order.created_at || null,
    filled_at: order.filled_at || null,
    order_type: order.type || null,
    broker_authoritative: true,
  };
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const credentials = await base44.asServiceRole.entities.BrokerCredential.filter({
      user_id: user.id,
      broker: 'alpaca',
      status: 'active',
    });
    const credential = credentials[0];
    if (!credential) {
      return Response.json({ error: 'Alpaca not connected' }, { status: 400 });
    }

    const orders = await getAlpacaOrders({
      apiKey: credential.api_key,
      secretKey: credential.api_secret,
      mode: credential.mode,
    }, { limit: 10 });

    return Response.json({
      as_of: new Date().toISOString(),
      mode: credential.mode,
      orders: orders.map(normalizeOrder),
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 502 });
  }
}
