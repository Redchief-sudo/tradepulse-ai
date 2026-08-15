import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    const credentials = await base44.asServiceRole.entities.MarketDataCredential.filter({ user_id: user.id, status: 'active' });
    return Response.json({
      ok: true,
      providers: credentials.reduce((result, credential) => {
        result[credential.provider] = {
          configured: true,
          key_suffix: credential.api_key ? `••••${credential.api_key.slice(-4)}` : '',
          api_plan: credential.api_plan || null,
          validated_at: credential.validated_at || null,
        };
        return result;
      }, {}),
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
