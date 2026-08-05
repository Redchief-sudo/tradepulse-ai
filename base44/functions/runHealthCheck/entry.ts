import { createClientFromRequest } from 'npm:@base44/sdk@0.8.40';
import { secrets } from 'base44:runtime';
import { getAlpacaAccount } from '../../shared/alpaca.ts';
import { AlpacaError } from '../../shared/alpacaErrors.ts';
import { sendTelegramMessage } from '../../shared/telegram.ts';

// System health check — monitors the autonomous trading system for degradation.
// Called on a schedule (every 5 minutes) or manually from the dashboard.
//
// Checks:
// 1. Stale scans — ScanRun with status 'running' but heartbeat not advancing
// 2. Pending orders — TradeIntents stuck in submitted/accepted for too long
// 3. Broker auth — Alpaca account endpoint returns 401/403
// 4. Reconciliation blocked — Holdings with reconciliation_blocked = true
// 5. Kill switch — User has kill_switch_reset_required = true
//
// Sends an email + Telegram alert for any issues found, and records AuditEvents.
export default async function(req) {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user || user.role !== 'admin') return Response.json({ error: 'Admin only' }, { status: 403 });

    const sr = base44.asServiceRole;
    const issues = [];

    // 1. STALE SCANS — ScanRun with status 'running' but heartbeat stopped
    const runningScans = await sr.entities.ScanRun.filter({ user_id: user.id, status: 'running' });
    const HEARTBEAT_STALE_MS = 3 * 60 * 1000;
    for (const scan of runningScans) {
      const lastBeat = scan.last_heartbeat_at ? new Date(scan.last_heartbeat_at).getTime() : new Date(scan.started_at).getTime();
      if (Date.now() - lastBeat > HEARTBEAT_STALE_MS) {
        issues.push({
          severity: 'critical',
          check: 'stale_scan',
          message: `Scan ${scan.scan_run_id} heartbeat stopped ${Math.round((Date.now() - lastBeat) / 1000)}s ago`,
          scan_run_id: scan.scan_run_id,
        });
        // Auto-recover: mark the stale scan as failed
        try {
          await sr.entities.ScanRun.update(scan.id, { status: 'failed', error: 'STALE_HEARTBEAT_HEALTH_CHECK', completed_at: new Date().toISOString() });
        } catch (e) {}
      }
    }

    // 2. PENDING ORDERS — TradeIntents stuck in submitted/accepted for > 5 minutes.
    // Uses submitted_at (broker submission time), not decision_timestamp (which
    // precedes AI/risk processing and can make orders appear older than they are).
    // (Fixes Rev.12 #22: health check used decision_timestamp for order age.)
    const intents = await sr.entities.TradeIntent.filter({ user_id: user.id });
    const PENDING_TIMEOUT_MS = 5 * 60 * 1000;
    const pendingIntents = intents.filter((i) => {
      if (!['submitted', 'accepted'].includes(i.status)) return false;
      // Use submitted_at if available, fall back to last_broker_update_at, then created_date
      const ageRef = i.submitted_at || i.last_broker_update_at || i.created_date;
      if (!ageRef) return false;
      return Date.now() - new Date(ageRef).getTime() > PENDING_TIMEOUT_MS;
    });
    for (const intent of pendingIntents) {
      const ageRef = intent.submitted_at || intent.last_broker_update_at || intent.created_date;
      const ageSec = Math.round((Date.now() - new Date(ageRef).getTime()) / 1000);
      issues.push({
        severity: 'warning',
        check: 'pending_order_timeout',
        message: `Order for ${intent.symbol} ${intent.side} pending for ${ageSec}s (status: ${intent.status})`,
        symbol: intent.symbol,
        trade_intent_id: intent.trade_intent_id,
      });
    }

    // 3. BROKER AUTH — check Alpaca account endpoint
    const brokerCreds = await sr.entities.BrokerCredential.filter({ user_id: user.id, status: 'active' });
    if (brokerCreds[0] && brokerCreds[0].broker === 'alpaca') {
      try {
        const acct = await getAlpacaAccount({ apiKey: brokerCreds[0].api_key, secretKey: brokerCreds[0].api_secret, mode: brokerCreds[0].mode });
        if (!acct || Number(acct.equity) <= 0) {
          issues.push({ severity: 'critical', check: 'broker_account_invalid', message: 'Alpaca account has no equity or is unreachable' });
        }
        // Check account trading-block flags
        if (acct.account_trading_blocked || acct.trades_blocked) {
          issues.push({ severity: 'critical', check: 'broker_account_blocked', message: 'Alpaca account is trading-blocked or trades-blocked' });
        }
      } catch (e) {
        if (e instanceof AlpacaError && e.isAuthError()) {
          issues.push({ severity: 'critical', check: 'broker_auth_failure', message: `Alpaca auth failed: ${e.message}`, request_id: e.requestId });
        } else {
          issues.push({ severity: 'warning', check: 'broker_unreachable', message: `Alpaca unreachable: ${e.message}` });
        }
      }
    }

    // 4. RECONCILIATION BLOCKED — holdings with reconciliation_blocked = true
    const holdings = await sr.entities.Holding.filter({ user_id: user.id });
    const blockedHoldings = holdings.filter((h) => h.reconciliation_blocked);
    for (const h of blockedHoldings) {
      issues.push({
        severity: 'warning',
        check: 'reconciliation_blocked',
        message: `${h.symbol} is reconciliation-blocked: ${h.reconciliation_blocked_reason || 'unknown reason'}`,
        symbol: h.symbol,
      });
    }

    // 5. KILL SWITCH — check if kill switch is active
    if (user.kill_switch_reset_required) {
      issues.push({
        severity: 'info',
        check: 'kill_switch_active',
        message: `Kill switch active: ${user.kill_switch_reason || 'unknown reason'}`,
      });
    }

    // Record AuditEvents for critical issues
    for (const issue of issues.filter((i) => i.severity === 'critical' || i.severity === 'warning')) {
      try {
        await sr.entities.AuditEvent.create({
          user_id: user.id,
          event_type: `health_check_${issue.check}`,
          severity: issue.severity,
          message: issue.message,
          details: JSON.stringify(issue),
        });
      } catch (e) {}
    }

    // Send alert if there are critical OR warning issues — warnings like
    // reconciliation-blocked positions and pending-order timeouts are
    // operationally important enough to alert immediately. (Fixes Rev.12 #24.)
    const alertableIssues = issues.filter((i) => i.severity === 'critical' || i.severity === 'warning');
    if (alertableIssues.length > 0) {
      const alertBody = alertableIssues.map((i) => `[${i.severity.toUpperCase()}] ${i.check}: ${i.message}`).join('\n');
      try {
        await sr.integrations.Core.SendEmail({
          to: user.email,
          subject: `TradePulse Health Alert: ${alertableIssues.length} issue(s)`,
          body: alertBody,
        });
      } catch (e) {
        try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', message: `Health check email failed: ${e.message}` }); } catch (ae) {}
      }

      if (user.telegram_chat_id && user.telegram_notifications_enabled) {
        try {
          const botToken = secrets.get('TELEGRAM_BOT_TOKEN');
          if (botToken) {
            await sendTelegramMessage(
              botToken,
              String(user.telegram_chat_id),
              `🚨 <b>TradePulse Health Alert</b>\n${alertableIssues.length} issue(s):\n${alertableIssues.map((i) => `• [${i.severity.toUpperCase()}] ${i.check}: ${i.message}`).join('\n')}`
            );
          }
        } catch (e) {
          try { await sr.entities.AuditEvent.create({ user_id: user.id, event_type: 'notification_failed', severity: 'warning', message: `Health check Telegram failed: ${e.message}` }); } catch (ae) {}
        }
      }
    }

    return Response.json({
      ok: true,
      healthy: alertableIssues.length === 0,
      issues,
      summary: {
        critical: issues.filter((i) => i.severity === 'critical').length,
        warnings: issues.filter((i) => i.severity === 'warning').length,
        info: issues.filter((i) => i.severity === 'info').length,
        total: issues.length,
      },
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
}