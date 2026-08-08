import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { authorizeSessionAction } from '../../shared/sessionControl.ts';

async function audit(sr, userId, action, message) {
  await sr.entities.AuditEvent.create({
    user_id: userId,
    event_type: 'trading_session_control',
    severity: 'info',
    entity_type: 'User',
    entity_id: userId,
    message,
    details: JSON.stringify({ action }),
  });
}

export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const caller = await base44.auth.me();
    if (!caller || caller.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const { action } = await req.json().catch(() => ({}));
    const sr = base44.asServiceRole;
    const user = await sr.entities.User.get(caller.id);
    if (!user) return Response.json({ error: 'User not found' }, { status: 404 });

    let unresolvedCount = 0;
    if (action === 'acknowledge_integrity_recovery') {
      const events = await sr.entities.SettlementEvent.filter({ user_id: user.id });
      unresolvedCount = events.filter((event) => event.status !== 'completed').length;
    }
    const decision = authorizeSessionAction(user, action, new Date(), unresolvedCount);
    if (!decision.allowed) return Response.json({ error: decision.reason, count: unresolvedCount || undefined }, { status: decision.reason === 'UNSUPPORTED_ACTION' ? 400 : 409 });
    await sr.entities.User.update(user.id, decision.patch);
    await audit(sr, user.id, action, `Trading session action authorized: ${action}`);

    const updated = await sr.entities.User.get(user.id);
    return Response.json({
      ok: true,
      trading_active: !!updated.trading_active,
      trading_session_state: updated.trading_session_state,
      kill_switch_reset_required: !!updated.kill_switch_reset_required,
      financial_integrity_manual_reenable_required: !!updated.financial_integrity_manual_reenable_required,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
