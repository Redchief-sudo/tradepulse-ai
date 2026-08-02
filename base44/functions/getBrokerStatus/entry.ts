import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Return masked broker status — NO secrets ever leave the server.
// The browser receives only: broker name, mode, connected flag, credential suffix,
// account_id suffix, buying power, and validation timestamp.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const sr = base44.asServiceRole;
    const creds = await sr.entities.BrokerCredential.filter({
      user_id: user.id,
      status: 'active',
    });

    if (!creds || creds.length === 0) {
      return Response.json({
        connected: false,
        broker: user.broker || '',
        broker_mode: user.broker_mode || 'paper',
      });
    }

    const cred = creds[0];
    return Response.json({
      connected: true,
      broker: cred.broker,
      broker_mode: cred.mode,
      credential_suffix: cred.api_secret ? `••••${cred.api_secret.slice(-4)}` : '',
      key_suffix: cred.api_key ? `••••${cred.api_key.slice(-4)}` : '',
      account_id: cred.account_id || null,
      buying_power: cred.buying_power || null,
      validated_at: cred.validated_at || null,
      permissions: cred.permissions || null,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}