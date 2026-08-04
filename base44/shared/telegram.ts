// Telegram Bot API client — sends messages via the Telegram Bot API.
// The bot token is stored as an app secret (TELEGRAM_BOT_TOKEN).
// The chat_id is stored on the user entity (non-sensitive).

export async function sendTelegramMessage(botToken: string, chatId: string, text: string) {
  if (!botToken || !chatId) {
    return { ok: false, error: 'Missing bot token or chat ID' };
  }
  try {
    const url = `https://api.telegram.org/bot${botToken}/sendMessage`;
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: 'HTML',
      }),
    });
    const data = await res.json();
    if (!data.ok) {
      return { ok: false, error: data.description || 'Telegram API error' };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}