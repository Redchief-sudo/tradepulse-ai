import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaAccount, getAlpacaPositions } from '../../shared/alpaca.ts';

function normalizePosition(position) {
  return {
    asset_id: position.asset_id || null,
    symbol: String(position.symbol || '').toUpperCase(),
    shares: Number(position.qty),
    avg_price: Number(position.avg_entry_price),
    current_price: Number(position.current_price),
    market_value: Number(position.market_value),
    unrealized_pl: Number(position.unrealized_pl),
    unrealized_pl_percent: Number(position.unrealized_plpc) * 100,
    asset_class: position.asset_class === 'crypto' ? 'crypto' : 'stocks',
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

    const connection = {
      apiKey: credential.api_key,
      secretKey: credential.api_secret,
      mode: credential.mode,
    };
    const [account, positions] = await Promise.all([
      getAlpacaAccount(connection),
      getAlpacaPositions(connection),
    ]);

    return Response.json({
      as_of: new Date().toISOString(),
      mode: credential.mode,
      account: {
        equity: Number(account.equity),
        cash: Number(account.cash),
        buying_power: Number(account.buying_power),
        last_equity: Number(account.last_equity),
      },
      positions: positions.map(normalizePosition),
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 502 });
  }
}
