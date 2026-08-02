import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaAccount } from '../../shared/alpaca.ts';

// Save broker credentials to the secure BrokerCredential entity (RLS-locked, service-role only).
// The secret is NEVER stored on or returned through the User object.
// Validates credentials against the broker before storing.
// Body: { broker, api_key, api_secret, mode, disconnect? }
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const body = await req.json().catch(() => ({}));

    // Disconnect: deactivate credentials and clear broker flags.
    if (body.disconnect) {
      const existing = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
      for (const c of existing) {
        await sr.entities.BrokerCredential.update(c.id, { status: 'disconnected' });
      }
      await base44.auth.updateMe({ broker: '', broker_connected: false, broker_mode: 'paper' });
      return Response.json({ ok: true, disconnected: true });
    }

    // Validate required fields.
    const broker = String(body.broker || '').toLowerCase();
    const apiKey = String(body.api_key || '').trim();
    const apiSecret = String(body.api_secret || '').trim();
    const mode = String(body.mode || 'paper');

    if (!broker || !apiKey || !apiSecret) {
      return Response.json({ error: 'broker, api_key, and api_secret are required' }, { status: 400 });
    }
    if (!['alpaca', 'ibkr', 'tradestation', 'custom'].includes(broker)) {
      return Response.json({ error: 'Unsupported broker' }, { status: 400 });
    }
    if (!['paper', 'live'].includes(mode)) {
      return Response.json({ error: 'mode must be paper or live' }, { status: 400 });
    }

    // Validate against the broker (Alpaca only for now).
    let accountInfo = null;
    if (broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey, secretKey: apiSecret, mode });
        if (!acct || !acct.id) {
          return Response.json({ error: 'Broker rejected credentials — check your API key and secret' }, { status: 401 });
        }
        accountInfo = {
          account_id: acct.id,
          buying_power: Number(acct.buying_power) || 0,
          equity: Number(acct.equity) || 0,
          status: acct.status,
          pattern_day_trader: acct.pattern_day_trader,
          trading_blocked: acct.trading_blocked,
        };
        if (acct.trading_blocked) {
          return Response.json({ error: 'Broker account is trading-blocked' }, { status: 403 });
        }
      } catch (e) {
        return Response.json({ error: `Broker validation failed: ${e.message}` }, { status: 401 });
      }
    }

    // Deactivate any existing active credentials for this user.
    const existing = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    for (const c of existing) {
      await sr.entities.BrokerCredential.update(c.id, { status: 'disconnected' });
    }

    // Store the new credential (service-role only — RLS blocks all client access).
    await sr.entities.BrokerCredential.create({
      user_id: user.id,
      broker,
      api_key: apiKey,
      api_secret: apiSecret,
      mode,
      account_id: accountInfo?.account_id || null,
      status: 'active',
      validated_at: new Date().toISOString(),
      buying_power: accountInfo?.buying_power || null,
      permissions: accountInfo?.status || null,
    });

    // Update the User's non-sensitive broker flags. The secret is NOT on the User object.
    await base44.auth.updateMe({
      broker,
      broker_connected: true,
      broker_mode: mode,
    });

    return Response.json({
      ok: true,
      broker,
      mode,
      account_id: accountInfo?.account_id,
      buying_power: accountInfo?.buying_power,
      equity: accountInfo?.equity,
      account_status: accountInfo?.status,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}