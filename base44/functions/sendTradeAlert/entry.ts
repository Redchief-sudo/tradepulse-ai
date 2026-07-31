import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';

// Sends an email alert to the calling user about a trade / stop-loss event.
// SendEmail reaches registered app users only.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await req.json().catch(() => ({}));
    const { subject, message } = body;
    if (!subject || !message) {
      return Response.json({ error: 'subject and message are required' }, { status: 400 });
    }

    await base44.integrations.Core.SendEmail({
      to: user.email,
      subject,
      body: message,
    });
    return Response.json({ ok: true, sent_to: user.email });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}