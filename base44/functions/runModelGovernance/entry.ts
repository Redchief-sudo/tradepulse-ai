import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { runGovernanceCycle } from '../../shared/modelGovernance.ts';

// Scheduled model governance cycle.
// Runs the full pipeline: collect outcomes → check rollback → generate candidate →
// walk-forward validate → promote only if proven → retain rollback history.
// This is the controlled self-evolution layer that makes the system safely adaptive.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const result = await runGovernanceCycle(base44.asServiceRole, user.id);
    return Response.json({ ok: true, ...result });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}