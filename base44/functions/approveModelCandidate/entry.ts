import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { getChampion, passesAllGates } from '../../shared/modelGovernance.ts';

// Manual approval of a governance candidate.
// Used when promotion_mode = 'manual_approval'. The governance cycle creates
// and validates a challenger but does NOT auto-promote it. This function lets
// the admin review the validation metrics and approve it if it passes all gates.
//
// Records the full immutable audit trail: who approved, when, rollback path.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const body = await req.json();
    const { candidate_id } = body;
    if (!candidate_id) return Response.json({ error: 'candidate_id is required' }, { status: 400 });

    const sr = base44.asServiceRole;

    const candidate = await sr.entities.StrategyModel.get(candidate_id);
    if (!candidate || candidate.user_id !== user.id) {
      return Response.json({ error: 'Candidate not found' }, { status: 404 });
    }
    if (candidate.status !== 'challenger') {
      return Response.json({ error: `Candidate is not a challenger (status: ${candidate.status})` }, { status: 400 });
    }

    // Verify it passed all validation gates
    const validation = candidate.out_of_sample_metrics ? JSON.parse(candidate.out_of_sample_metrics) : null;
    if (!validation || !passesAllGates(validation)) {
      return Response.json({ error: 'Candidate did not pass validation gates', validation }, { status: 400 });
    }

    // Find the current champion for this regime and retire it
    const regime = candidate.regime || 'all';
    const champion = await getChampion(sr, user.id, regime);
    if (champion && champion.id !== candidate.id) {
      await sr.entities.StrategyModel.update(champion.id, {
        status: 'retired',
        retired_at: new Date().toISOString(),
      });
    }

    // Promote the candidate with full audit trail
    await sr.entities.StrategyModel.update(candidate.id, {
      status: 'champion',
      approval_status: 'approved',
      promoted_at: new Date().toISOString(),
      approved_by: user.id,
      rollback_path: champion
        ? `${champion.rollback_path || champion.version} → ${candidate.version}`
        : candidate.version,
    });

    // Update user.ml_weights cache only for global champion
    if (regime === 'all') {
      await sr.entities.User.update(user.id, { ml_weights: candidate.weights });
    }

    return Response.json({
      ok: true,
      promoted: candidate.version,
      regime,
      approved_by: user.id,
      validation,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}