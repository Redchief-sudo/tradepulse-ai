import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { settleTrade } from '../../shared/execution.ts';

// Canonical trade execution endpoint — the single entry point every trading
// surface calls. It validates, submits to the broker (when connected), polls
// the fill, and settles accounting atomically from the confirmed fill.
// A rejected/pending broker order returns a non-2xx status and mutates NOTHING.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const intent = await req.json().catch(() => ({}));
    // Inject the server-side Finnhub key so the execution gateway can fetch the
    // authoritative quote without depending on a pre-existing snapshot. The key
    // never leaves the server.
    intent.finnhub_key = secrets.get('FINNHUB_API_KEY');
    const result = await settleTrade(base44, user, intent);

    const code = result.status === 'rejected' || result.status === 'invalid'
      ? 402
      : result.status === 'pending'
      ? 202
      : 200;
    return Response.json(result, { status: code });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}