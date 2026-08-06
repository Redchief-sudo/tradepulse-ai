import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Auto-promotion gate: evaluates paper-trading performance and promotes the
// user's broker credentials from paper to LIVE when both thresholds are met.
//
// Thresholds (on the User entity):
//   auto_promote_min_trades   — minimum closed (sell) trades with a realized P&L
//   auto_promote_min_win_rate — minimum win-rate percentage (0-100)
//
// Safety:
//   - Only fires when auto_promote_enabled is true
//   - Only fires once (auto_promote_triggered_at prevents re-triggering)
//   - Only fires when an active broker credential is in PAPER mode
//   - Promotes the BrokerCredential.mode to 'live' and mirrors it on User.broker_mode
//   - Records a critical AuditEvent and emails the user
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    // Gate 1: feature must be enabled
    if (!user.auto_promote_enabled) {
      return Response.json({ ok: true, skipped: true, reason: 'Auto-promote disabled' });
    }
    // Gate 2: one-shot — never re-trigger after the first promotion
    if (user.auto_promote_triggered_at) {
      return Response.json({ ok: true, skipped: true, reason: 'Already promoted' });
    }

    const sr = base44.asServiceRole;

    // Gate 3: must have an active broker credential in PAPER mode
    const creds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (!creds.length) {
      return Response.json({ ok: true, skipped: true, reason: 'No active broker credential' });
    }
    const cred = creds[0];
    if (cred.mode !== 'paper') {
      return Response.json({ ok: true, skipped: true, reason: `Already in ${cred.mode} mode` });
    }

    const minTrades = Number(user.auto_promote_min_trades) || 20;
    const minWinRate = Number(user.auto_promote_min_win_rate) || 60;

    // Sample: closed positions (sells with a realized P&L) — the only trades
    // with a measurable win/loss outcome. Open buys don't prove anything.
    const sells = await sr.entities.Trade.filter({ user_id: user.id, action: 'sell' }, '-created_date', 500);
    const closedSells = sells.filter((t) => t.realized_pnl != null);

    if (closedSells.length < minTrades) {
      return Response.json({
        ok: true,
        skipped: true,
        reason: 'Not enough closed trades',
        trade_count: closedSells.length,
        required: minTrades,
      });
    }

    const wins = closedSells.filter((t) => (t.realized_pnl || 0) > 0).length;
    const winRate = (wins / closedSells.length) * 100;
    const winRateRounded = Math.round(winRate * 10) / 10;

    if (winRate < minWinRate) {
      return Response.json({
        ok: true,
        skipped: true,
        reason: 'Win rate below threshold',
        win_rate: winRateRounded,
        required: minWinRate,
        trade_count: closedSells.length,
      });
    }

    // PROMOTE — flip the credential and mirror the mode on the user.
    await sr.entities.BrokerCredential.update(cred.id, { mode: 'live' });
    await sr.entities.User.update(user.id, {
      broker_mode: 'live',
      auto_promote_triggered_at: new Date().toISOString(),
    });

    await sr.entities.AuditEvent.create({
      user_id: user.id,
      event_type: 'auto_promote_to_live',
      severity: 'critical',
      message: `Auto-promoted paper → LIVE: ${closedSells.length} closed trades, ${winRateRounded}% win rate (thresholds: ${minTrades} trades, ${minWinRate}% win rate)`,
    });

    try {
      await sr.integrations.Core.SendEmail({
        to: user.email,
        subject: 'TradePulse: Auto-promoted to LIVE trading',
        body: `Your account has been automatically promoted from paper to live trading.\n\nPaper performance:\n- Closed trades: ${closedSells.length}\n- Win rate: ${winRateRounded}%\n- Thresholds: ${minTrades} trades, ${minWinRate}% win rate\n\nLive orders will now be placed with real capital. Monitor your positions closely.`,
      });
    } catch (e) {
      await sr.entities.AuditEvent.create({
        user_id: user.id,
        event_type: 'notification_failed',
        severity: 'warning',
        message: `Auto-promote email failed: ${e.message}`,
      });
    }

    return Response.json({
      ok: true,
      promoted: true,
      trade_count: closedSells.length,
      win_rate: winRateRounded,
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}