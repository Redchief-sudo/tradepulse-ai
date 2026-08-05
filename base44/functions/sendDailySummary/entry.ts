import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { sendTelegramMessage } from '../../shared/telegram.ts';
import { getAlpacaAccount } from '../../shared/alpaca.ts';

// Sends a daily P&L summary via email + Telegram.
// Called when the user presses "Stop" or automatically at market close (4 PM ET).
// USER-SCOPED: all queries filter by user_id.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Not authenticated' }, { status: 401 });

    const sr = base44.asServiceRole;

    // USER-SCOPED: only this user's data
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const trades = await sr.entities.Trade.filter({ user_id: user.id });
    const scanRuns = await sr.entities.ScanRun.filter({ user_id: user.id });

    // Portfolio metrics — app-portfolio estimate from holdings × current price.
    // For broker-connected users, also fetch the authoritative broker equity.
    // (Fixes Rev.9 defect #18: daily summary was calculated only from holdings,
    // which can diverge from actual broker equity due to stale prices, cash,
    // fees, and unreconciled fills.)
    const portfolioValue = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price), 0);
    const costBasis = holdings.reduce((s, h) => s + h.shares * h.avg_price, 0);
    const totalPL = portfolioValue - costBasis;
    const totalPLPct = costBasis > 0 ? (totalPL / costBasis) * 100 : 0;
    const dayPL = holdings.reduce((s, h) => s + h.shares * (h.current_price || h.avg_price) * ((h.day_change_percent || 0) / 100), 0);
    const prevValue = portfolioValue - dayPL;
    const dayPLPct = prevValue > 0 ? (dayPL / prevValue) * 100 : 0;

    // Authoritative broker equity (when available)
    let brokerEquity = null;
    let brokerPrevCloseEquity = null;
    let brokerEquityLabel = 'Application portfolio estimate (not reconciled to broker)';
    const brokerCreds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
        brokerEquity = Number(acct.equity);
        brokerPrevCloseEquity = Number(acct.last_equity) || null;
        brokerEquityLabel = 'Broker-authoritative equity (Alpaca)';
      } catch (e) {
        brokerEquityLabel = 'Application portfolio estimate (broker unreachable)';
      }
    }

    // Today's activity
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);
    const todayTrades = trades.filter((t) => new Date(t.created_date) >= todayStart);
    const todayScans = scanRuns.filter((s) => new Date(s.started_at) >= todayStart);

    // Best / worst performers today
    const performers = holdings
      .map((h) => ({ symbol: h.symbol, change: h.day_change_percent || 0 }))
      .sort((a, b) => b.change - a.change);
    const best = performers[0] || null;
    const worst = performers[performers.length - 1] || null;

    // Separate broker and app P&L metrics — they measure different things and
    // can diverge due to cash, fees, unreconciled fills, and stale prices.
    // (Fixes Rev.9 defect #18: daily summary mixed broker and app P&L without
    // distinguishing which is authoritative.)
    const brokerDayPL = (brokerEquity != null && brokerPrevCloseEquity != null) ? brokerEquity - brokerPrevCloseEquity : null;
    const appUnrealizedPL = totalPL;
    const appRealizedPL = todayTrades
      .filter((t) => t.action === 'sell')
      .reduce((s, t) => s + (t.realized_pnl || 0), 0);
    const reconciliationDiff = (brokerEquity != null) ? brokerEquity - portfolioValue : null;

    const summary = {
      portfolioValue,
      brokerEquity,
      brokerPrevCloseEquity,
      brokerDayPL,
      brokerEquityLabel,
      costBasis,
      appUnrealizedPL,
      appRealizedPL,
      reconciliationDiff,
      totalPL,
      totalPLPct,
      dayPL,
      dayPLPct,
      tradesToday: todayTrades.length,
      scansToday: todayScans.length,
      positions: holdings.length,
      best,
      worst,
    };

    const fmt = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n || 0);
    const fmtPct = (n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;

    // Email
    const emailLines = [
      `TradePulse Daily Summary`,
      `${'='.repeat(40)}`,
      `--- Broker (Authoritative) ---`,
      brokerEquity != null ? `Alpaca Equity: ${fmt(brokerEquity)}` : `Alpaca: ${brokerEquityLabel}`,
      brokerPrevCloseEquity != null ? `Alpaca Prev Close: ${fmt(brokerPrevCloseEquity)}` : '',
      brokerDayPL != null ? `Alpaca Day P&L: ${brokerDayPL >= 0 ? '+' : ''}${fmt(brokerDayPL)}` : '',
      `--- Application (Estimate) ---`,
      `App Position Value: ${fmt(portfolioValue)}`,
      `App Unrealized P&L: ${totalPL >= 0 ? '+' : ''}${fmt(totalPL)} (${fmtPct(totalPLPct)})`,
      `App Realized P&L Today: ${appRealizedPL >= 0 ? '+' : ''}${fmt(appRealizedPL)}`,
      `App Day P&L (est): ${dayPL >= 0 ? '+' : ''}${fmt(dayPL)} (${fmtPct(dayPLPct)})`,
      reconciliationDiff != null ? `Reconciliation Diff: ${fmt(reconciliationDiff)} (broker - app)` : '',
      `--- Activity ---`,
      `Trades Today: ${todayTrades.length}`,
      `Scans Today: ${todayScans.length}`,
      `Open Positions: ${holdings.length}`,
      best ? `Best Performer: ${best.symbol} ${fmtPct(best.change)}` : '',
      worst ? `Worst Performer: ${worst.symbol} ${fmtPct(worst.change)}` : '',
      `${'='.repeat(40)}`,
    ].filter(Boolean).join('\n');

    try {
      await sr.integrations.Core.SendEmail({
        to: user.email,
        subject: `TradePulse Daily Summary: ${dayPL >= 0 ? '+' : ''}${fmt(dayPL)} (${fmtPct(dayPLPct)})`,
        body: emailLines,
      });
    } catch (e) { /* non-fatal */ }

    // Telegram
    if (user.telegram_chat_id && user.telegram_notifications_enabled) {
      try {
        const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
        if (botToken) {
          const tgLines = [
            `📊 <b>TradePulse Daily Summary</b>`,
            `━━━━━━━━━━━━━━━━━━━`,
            brokerEquity != null ? `<b>Broker Equity:</b> ${fmt(brokerEquity)}` : `<b>Portfolio:</b> ${fmt(portfolioValue)}`,
            brokerDayPL != null ? `Broker Day P&L: ${brokerDayPL >= 0 ? '🟢 +' : '🔴 '}${fmt(brokerDayPL)}` : '',
            `App Value: ${fmt(portfolioValue)}`,
            `App Unrealized: ${totalPL >= 0 ? '+' : ''}${fmt(totalPL)}`,
            `App Realized Today: ${appRealizedPL >= 0 ? '+' : ''}${fmt(appRealizedPL)}`,
            reconciliationDiff != null ? `Recon Diff: ${fmt(reconciliationDiff)}` : '',
            `Trades Today: ${todayTrades.length}`,
            best ? `Best: ${best.symbol} ${fmtPct(best.change)}` : '',
            worst ? `Worst: ${worst.symbol} ${fmtPct(worst.change)}` : '',
          ].filter(Boolean).join('\n');
          await sendTelegramMessage(botToken, String(user.telegram_chat_id), tgLines);
        }
      } catch (e) { /* non-fatal */ }
    }

    return Response.json({ ok: true, summary });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}