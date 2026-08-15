import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

async function validateCoinGecko(apiKey) {
  const query = '?ids=bitcoin&vs_currencies=usd';
  const attempts = [
    { plan: 'demo', url: `https://api.coingecko.com/api/v3/simple/price${query}`, header: 'x-cg-demo-api-key' },
    { plan: 'pro', url: `https://pro-api.coingecko.com/api/v3/simple/price${query}`, header: 'x-cg-pro-api-key' },
  ];
  const failures = [];
  for (const attempt of attempts) {
    try {
      const response = await fetch(attempt.url, { headers: { Accept: 'application/json', [attempt.header]: apiKey } });
      const body = await response.json().catch(() => ({}));
      if (response.ok && Number(body?.bitcoin?.usd) > 0) return { plan: attempt.plan };
      failures.push(`${attempt.plan}=HTTP ${response.status}`);
    } catch (error) {
      failures.push(`${attempt.plan}=${error.message}`);
    }
  }
  throw new Error(`CoinGecko rejected the key (${failures.join(', ')})`);
}

async function validateCoinMarketCap(apiKey) {
  const response = await fetch('https://pro-api.coinmarketcap.com/v1/key/info', {
    headers: { Accept: 'application/json', 'X-CMC_PRO_API_KEY': apiKey },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || Number(body?.status?.error_code || 0) !== 0) {
    throw new Error(`CoinMarketCap rejected the key: ${body?.status?.error_message || `HTTP ${response.status}`}`);
  }
  return { plan: 'standard' };
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const body = await req.json().catch(() => ({}));
    const provider = String(body.provider || '').toLowerCase();
    const apiKey = String(body.api_key || '').trim();
    if (!['coingecko', 'coinmarketcap'].includes(provider)) return Response.json({ error: 'Unsupported market-data provider' }, { status: 400 });

    const sr = base44.asServiceRole;
    const existing = await sr.entities.MarketDataCredential.filter({ user_id: user.id, provider });
    if (body.disconnect === true) {
      for (const credential of existing) await sr.entities.MarketDataCredential.update(credential.id, { status: 'disconnected' });
      return Response.json({ ok: true, provider, disconnected: true });
    }
    if (!apiKey) return Response.json({ error: 'API key is required' }, { status: 400 });

    let validation;
    try {
      validation = provider === 'coingecko' ? await validateCoinGecko(apiKey) : await validateCoinMarketCap(apiKey);
    } catch (error) {
      return Response.json({ error: error.message }, { status: 401 });
    }
    for (const credential of existing) {
      if (credential.status === 'active') await sr.entities.MarketDataCredential.update(credential.id, { status: 'disconnected' });
    }
    const validatedAt = new Date().toISOString();
    await sr.entities.MarketDataCredential.create({ user_id: user.id, provider, api_key: apiKey, api_plan: validation.plan, status: 'active', validated_at: validatedAt });
    return Response.json({ ok: true, provider, api_plan: validation.plan, validated_at: validatedAt });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
