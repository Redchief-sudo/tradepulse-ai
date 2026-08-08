import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { getAlpacaClock } from '../../shared/alpaca.ts';
import { authorizeSessionAction } from '../../shared/sessionControl.ts';
import { nySessionDateStr } from '../../shared/marketHours.ts';

// Alpaca's clock—not cron timing—proves the regular session is open. A
// date-scoped audit marker prevents duplicate starts and Telegram messages.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const caller = await base44.auth.me();
    if (!caller || caller.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });
    const sr = base44.asServiceRole;
    const user = await sr.entities.User.get(caller.id);
    const sessionDate = nySessionDateStr();
    const correlationId = `market-open-${sessionDate}`;
    const priorStarted = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_open_bot_started', correlation_id: correlationId });
    const priorBlocked = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_open_bot_blocked', correlation_id: correlationId });
    if (priorStarted.length || priorBlocked.length) return Response.json({ ok: true, skipped: 'already_processed', session_date: sessionDate });

    const credentials = await sr.entities.BrokerCredential.filter({ user_id: user.id, broker: 'alpaca', status: 'active' });
    const credential = credentials[0];
    if (!credential) return Response.json({ error: 'ALPACA_CREDENTIALS_REQUIRED_FOR_MARKET_OPEN_AUTHORITY' }, { status: 409 });
    const clock = await getAlpacaClock({ apiKey: credential.api_key, secretKey: credential.api_secret, mode: credential.mode });
    if (!clock?.is_open) return Response.json({ ok: true, skipped: 'alpaca_market_not_open', session_date: sessionDate });

    let transition = 'already_active';
    if (!user.trading_active || user.trading_session_state !== 'active') {
      const decision = authorizeSessionAction(user, 'start');
      if (!decision.allowed) transition = `blocked:${decision.reason}`;
      else {
        await sr.entities.User.update(user.id, decision.patch);
        transition = 'started';
      }
    }

    let scan = 'not_started';
    if (transition === 'started' || transition === 'already_active') {
      try {
        await base44.functions.invoke('runScanCoordinator', {});
        scan = 'coordinator_invoked';
      } catch (error) {
        scan = `scheduled_retry:${error.message}`;
      }
    }

    let telegramSent = false;
    if (user.telegram_chat_id && user.telegram_notifications_enabled) {
      const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
      if (botToken) {
        const message = transition.startsWith('blocked:')
          ? `⚠️ <b>TradePulse market open</b>\nBot was not started. ${transition.slice(8)}`
          : `🔔 <b>Market Open</b> — Alpaca confirms the regular session is open.\n🤖 Bot status: ${transition === 'started' ? 'started' : 'already active'}\n🔎 Scan status: ${scan}`;
        const sent = await sendTelegramMessage(botToken, String(user.telegram_chat_id), message);
        telegramSent = sent.ok;
      }
    }

    await sr.entities.AuditEvent.create({
      user_id: user.id, event_type: transition.startsWith('blocked:') ? 'market_open_bot_blocked' : 'market_open_bot_started',
      severity: transition.startsWith('blocked:') ? 'warning' : 'info', correlation_id: correlationId,
      entity_type: 'User', entity_id: user.id,
      message: `Market-open bot transition: ${transition}; scan=${scan}; telegram=${telegramSent}`,
      details: JSON.stringify({ transition, scan, telegramSent, sessionDate, alpacaTimestamp: clock.timestamp || null }),
    });
    return Response.json({ ok: !transition.startsWith('blocked:'), transition, scan, telegram_sent: telegramSent, session_date: sessionDate });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
