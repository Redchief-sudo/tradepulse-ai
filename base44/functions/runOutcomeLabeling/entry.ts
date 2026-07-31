import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { labelOutcomes } from '../../shared/outcomes.ts';

// Scheduled outcome labeling. Computes forward/realized returns, benchmark-relative
// return, and max adverse/favorable excursion for every executed AI buy decision,
// using the Fill ledger + the PriceSnapshot time series. This is the closed feedback
// loop that makes the AI's intelligence measurable.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const result = await labelOutcomes(base44.asServiceRole);
    return Response.json({ ok: true, ...result });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}