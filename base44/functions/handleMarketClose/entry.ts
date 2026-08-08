import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { updateSessionState, SESSION_STATES } from '../../shared/sessionState.ts';
import { nySessionDateStr } from '../../shared/marketHours.ts';
import { getAlpacaClock } from '../../shared/alpaca.ts';

// Alpaca's clock authorizes the equity-session close transition. If broker
// authority is unavailable, the recurring workflow retries without guessing.
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
    const prior = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_close_asset_sessions_updated', correlation_id: correlationId });
    if (prior.length) return Response.json({ ok: true, skipped: 'already_processed', session_date: sessionDate });
    const openMarkers = await sr.entities.AuditEvent.filter({ user_id: user.id, event_type: 'market_open_bot_started', correlation_id: `market-open-${sessionDate}` });
    if (!openMarkers.length && user.trading_session_state !== SESSION_STATES.ACTIVE) {
      return Response.json({ ok: true, skipped: 'no_open_session_for_date', session_date: sessionDate });
    }
    const credentials = await sr.entities.BrokerCredential.filter({ user_id: user.id, broker: 'alpaca', status: 'active' });
    const credential = credentials[0];
    if (!credential) return Response.json({ error: 'ALPACA_CREDENTIALS_REQUIRED_FOR_MARKET_CLOSE_AUTHORITY' }, { status: 409 });
    const clock = await getAlpacaClock({ apiKey: credential.api_key, secretKey: credential.api_secret, mode: credential.mode });
    if (clock?.is_open) return Response.json({ ok: true, skipped: 'alpaca_market_still_open', next_close: clock.next_close || null, session_date: sessionDate });

    let transition = 'already_stopped';
    if (user.trading_active && user.trading_session_state === SESSION_STATES.ACTIVE) {
      await updateSessionState(sr, user.id, SESSION_STATES.MARKET_CLOSED, 'US regular session closed; continuous assets remain enabled');
      transition = 'regular_assets_paused_continuous_assets_active';
    }

    const continuousAssetsActive = Boolean(user.trading_active)
      && (transition === 'regular_assets_paused_continuous_assets_active' || user.trading_session_state === SESSION_STATES.MARKET_CLOSED);
    let summary = null;
    let summaryError = null;
    try {
      const response = await base44.functions.invoke('sendDailySummary', { market_close: true, continuous_assets_active: continuousAssetsActive });
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
        const sessionMessage = continuousAssetsActive
          ? `⏸️ Regular-session assets paused for ${sessionDate}.\n🟢 24/7 assets remain active.`
          : `All autonomous trading remains stopped for ${sessionDate}.`;
        const sent = await sendTelegramMessage(botToken, String(user.telegram_chat_id), `🔕 <b>US Market Closed</b>\n${sessionMessage}${detail}`);
        fallbackTelegramSent = sent.ok;
      }
    }

    await sr.entities.AuditEvent.create({
      user_id: user.id, event_type: 'market_close_asset_sessions_updated', severity: (summaryError || closeTelegramMissing) && !fallbackTelegramSent ? 'warning' : 'info',
      correlation_id: correlationId, entity_type: 'User', entity_id: user.id,
      message: `Market-close session transition: ${transition}; summary=${summaryError ? 'failed' : 'sent'}`,
      details: JSON.stringify({ sessionDate, transition, summaryError, closeTelegramMissing, fallbackTelegramSent, alpacaTimestamp: clock?.timestamp || null, alpacaNextOpen: clock?.next_open || null }),
    });
    return Response.json({ ok: !summaryError, transition, continuous_assets_active: continuousAssetsActive, summary, summary_error: summaryError, fallback_telegram_sent: fallbackTelegramSent });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}
