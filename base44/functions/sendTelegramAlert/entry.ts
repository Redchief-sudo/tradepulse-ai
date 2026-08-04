import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';

// Sends a Telegram message to the calling user's chat.
// The bot token is read from the TELEGRAM_BOT_TOKEN secret.
// The chat_id is read from the user's profile (telegram_chat_id).
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const message = body.message || body.text;
    if (!message) {
      return Response.json({ error: 'message is required' }, { status: 400 });
    }

    const chatId = user.telegram_chat_id;
    if (!chatId) {
      return Response.json({ error: 'No Telegram chat ID configured. Set it in Settings.' }, { status: 400 });
    }

    const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
    if (!botToken) {
      return Response.json({ error: 'Telegram bot token not configured. Ask the app admin to set TELEGRAM_BOT_TOKEN.' }, { status: 500 });
    }

    const result = await sendTelegramMessage(botToken, String(chatId), message);
    if (!result.ok) {
      return Response.json({ error: result.error }, { status: 500 });
    }
    return Response.json({ ok: true, sent_to: chatId });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}