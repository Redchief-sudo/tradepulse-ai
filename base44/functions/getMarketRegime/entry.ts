import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { classifyRegimeFromSnapshots } from '../../shared/regime.ts';

// Returns the current deterministic market regime (computed from SPY price
// snapshots). Used by the frontend RegimeBanner and as an override of the
// LLM-estimated regime in the autonomous scan.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    const regime = await classifyRegimeFromSnapshots(base44.asServiceRole, user.id);
    return Response.json({ ok: true, regime });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
