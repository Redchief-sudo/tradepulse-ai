import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaActivities } from '../../shared/alpaca.ts';

function normalizeFill(activity) {
  const shares = Number(activity.qty);
  const price = Number(activity.price);
  return {
    id: activity.id,
    broker_order_id: activity.order_id || null,
    symbol: String(activity.symbol || '').toUpperCase(),
    action: String(activity.side || '').toLowerCase(),
    shares,
    price,
    total_value: shares * price,
    filled_at: activity.transaction_time || activity.date || null,
    status: activity.type || activity.activity_type || 'fill',
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

    const activities = await getAlpacaActivities({
      apiKey: credential.api_key,
      secretKey: credential.api_secret,
      mode: credential.mode,
      activityType: 'FILL',
      pageSize: 10,
      direction: 'desc',
    });

    return Response.json({
      as_of: new Date().toISOString(),
      mode: credential.mode,
      fills: activities.map(normalizeFill).slice(0, 10),
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 502 });
  }
}
