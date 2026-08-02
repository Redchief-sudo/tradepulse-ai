import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getAlpacaAccount } from '../../shared/alpaca.ts';

// Validate broker credentials by contacting the broker's account endpoint.
// Does NOT store credentials — use saveBrokerCredentials for that.
// Body: { broker, api_key, api_secret, mode }
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const broker = String(body.broker || '').toLowerCase();
    const apiKey = String(body.api_key || '').trim();
    const apiSecret = String(body.api_secret || '').trim();
    const mode = String(body.mode || 'paper');

    if (!broker || !apiKey || !apiSecret) {
      return Response.json({ ok: false, message: 'Broker, API key, and API secret are required.' }, { status: 400 });
    }

    if (broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey, secretKey: apiSecret, mode });
        if (!acct || !acct.id) {
          return Response.json({ ok: false, message: 'Broker rejected credentials — check your API key and secret.' });
        }
        return Response.json({
          ok: true,
          message: `Connected to Alpaca ${mode} account ${acct.id.slice(-4)}. Equity: $${Number(acct.equity || 0).toFixed(2)}.`,
          account_id: acct.id,
          account_status: acct.status,
          buying_power: Number(acct.buying_power) || 0,
          equity: Number(acct.equity) || 0,
          pattern_day_trader: acct.pattern_day_trader,
          trading_blocked: acct.trading_blocked,
        });
      } catch (e) {
        return Response.json({ ok: false, message: `Broker validation failed: ${e.message}` });
      }
    }

    return Response.json({ ok: false, message: `Validation not supported for broker: ${broker}` });
  } catch (error) {
    return Response.json({ ok: false, message: error.message }, { status: 500 });
  }
}