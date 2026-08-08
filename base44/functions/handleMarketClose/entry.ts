import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { authorizeSessionAction } from '../../shared/sessionControl.ts';
import { nySessionDateStr } from '../../shared/marketHours.ts';

// Stopping is protective and remains safe if broker authority is unavailable.
// The daily summary supplies the normal Telegram close notification; a fallback
// is sent only when summary generation fails.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const caller = await base44.auth.me();
    if (!caller || caller.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const sr = base44.asServiceRole;
    const user = await sr.entities.User.get(caller.id);
    const sessionDate = nySessionDateStr();
    const correlationId = `market-close-${sessionDate}`;
    const prior = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_close_bot_stopped', correlation_id: correlationId });
    if (prior.length) return Response.json({ ok: true, skipped: 'already_processed', session_date: sessionDate });
    const openMarkers = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_open_bot_started', correlation_id: `market-open-${sessionDate}` });
    if (!openMarkers.length && !user.trading_active) {
      return Response.json({ ok: true, skipped: 'no_open_session_for_date', session_date: sessionDate });
    }

    const decision = authorizeSessionAction(user, 'stop');
    await sr.entities.User.update(user.id, decision.patch);

    let summary = null;
    let summaryError = null;
    try {
      const response = await base44.functions.invoke('sendDailySummary', {});
      summary = response?.data || response;
      if (!summary?.ok) summaryError = summary?.error || 'Daily summary returned no success';
    } catch (error) {
      summaryError = error.message;
    }

    const closeTelegramMissing = Boolean(user.telegram_chat_id && user.telegram_notifications_enabled && !summary?.telegramSent);
    let fallbackTelegramSent = false;
    if ((summaryError || closeTelegramMissing) && user.telegram_chat_id && user.telegram_notifications_enabled) {
      const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
      if (botToken) {
        const detail = summaryError ? `\n⚠️ Daily summary failed: ${summaryError}` : '';
        const sent = await sendTelegramMessage(botToken, String(user.telegram_chat_id), `🔕 <b>Market Closed</b>\n🤖 TradePulse bot stopped for ${sessionDate}.${detail}`);
        fallbackTelegramSent = sent.ok;
      }
    }

    await sr.entities.AuditEvent.create({
      user_id: user.id, event_type: 'market_close_bot_stopped', severity: (summaryError || closeTelegramMissing) && !fallbackTelegramSent ? 'warning' : 'info',
      correlation_id: correlationId, entity_type: 'User', entity_id: user.id,
      message: `Market-close bot stopped; summary=${summaryError ? 'failed' : 'sent'}`,
      details: JSON.stringify({ sessionDate, summaryError, closeTelegramMissing, fallbackTelegramSent }),
    });
    return Response.json({ ok: !summaryError, stopped: true, summary, summary_error: summaryError, fallback_telegram_sent: fallbackTelegramSent });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
