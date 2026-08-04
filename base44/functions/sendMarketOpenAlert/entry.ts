import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { classifyRegimeFromSnapshots } from '../../shared/regime.ts';

// Sends a Telegram "market open" notification with a portfolio summary.
// Triggered by the "Market Open Alert" workflow at 9:30 AM ET (6:30 AM Pacific)
// on weekdays — right when the US regular session begins.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const chatId = user.telegram_chat_id;
    if (!chatId || !user.telegram_notifications_enabled) {
      return Response.json({ ok: false, skipped: 'Telegram not configured or disabled' });
    }

    const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
    if (!botToken) {
      return Response.json({ error: 'Telegram bot token not configured' }, { status: 500 });
    }

    const sr = base44.asServiceRole;
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });

    const totalValue = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    const totalInvested = holdings.reduce((s, h) => s + h.shares * h.avg_price, 0);
    const totalPL = totalValue - totalInvested;
    const totalPLPercent = totalInvested > 0 ? (totalPL / totalInvested) * 100 : 0;

    // Market regime from the latest price snapshots
    let regimeText = 'Unknown';
    try {
      const regime = await classifyRegimeFromSnapshots(sr);
      regimeText = regime.market_regime || 'Unknown';
    } catch (e) { /* non-fatal */ }

    const plSign = totalPL >= 0 ? '+' : '';
    const plEmoji = totalPL >= 0 ? '📈' : '📉';

    const lines = [
      `🔔 <b>Market Open</b> — 9:30 AM ET`,
      ``,
      `📊 <b>Portfolio Summary</b>`,
      `Value: $${totalValue.toFixed(2)}`,
      `${plEmoji} P&amp;L: ${plSign}$${totalPL.toFixed(2)} (${plSign}${totalPLPercent.toFixed(2)}%)`,
      `Positions: ${holdings.length}`,
      `Regime: ${regimeText}`,
      ``,
      `🤖 Intraday AI scan starting now...`,
    ];

    await sendTelegramMessage(botToken, String(chatId), lines.join('\n'));
    return Response.json({ ok: true, sent_to: chatId });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}