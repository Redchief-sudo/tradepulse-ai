import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import {
  Settings as SettingsIcon,
  Key,
  Eye,
  EyeOff,
  CheckCircle2,
  Save,
  Loader2,
  Shield,
  ShieldCheck,
  AlertTriangle,
  Link2,
  Unlink,
  Rocket,
  FlaskConical,
  UserCheck,
  Zap,
  GitBranch,
  Send,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { cn } from '@/lib/utils';
import TradingProfileSelector from '@/components/TradingProfileSelector';

const BROKERS = [
  {
    id: 'ibkr',
    name: 'Interactive Brokers',
    badge: '🏆 Best Overall — Multi-Asset',
    tagline: 'True institutional multi-asset coverage across 150+ global markets',
    assets: 'Stocks, Options, Futures, Forex (20+ currencies), Crypto, Commodities, Fixed Income / Bonds',
    why: [
      'Direct Market Access (DMA) & Level 3 order book depth for institutional-grade microstructure signals',
      'Native algorithmic order slicing (TWAP, VWAP, Accumulate/Distribute) built into the execution engine',
      'Unlimited paper trading accounts (Sandbox) for simulated testing',
    ],
    api: 'Modern REST API + WebSockets, or Python TWS API gateway (IBKR Client Portal Web API / TWS API)',
    enabled: false,
  },
  {
    id: 'alpaca',
    name: 'Alpaca Markets',
    badge: '🚀 Best Developer Experience',
    tagline: 'Developer-first, cloud-native API — easy to wire up without local software gateways',
    assets: 'US Equities (Stocks & ETFs), Options, and 50+ Cryptocurrency pairs (limited direct Forex/Commodity futures vs IBKR)',
    why: [
      'Free, instantaneous Paper Trading sandbox with zero friction',
      '100% cloud-based REST API and WebSockets — no desktop terminal required',
      'Built specifically for automated trading bots and programmatic execution',
    ],
    api: 'Cloud REST API + WebSockets',
    enabled: true,
  },
  {
    id: 'tradestation',
    name: 'TradeStation',
    badge: '🌐 Best for Futures & Commodities',
    tagline: 'High-frequency API access for macro commodities and index futures',
    assets: 'Stocks, Options, Futures, Forex, and Crypto',
    why: [
      'Excellent futures execution and institutional order routing',
      'Strong for Crude Oil, Gold, Wheat and Index Futures (E-mini S&P, Nasdaq)',
      'Real-time streaming capabilities',
    ],
    api: 'REST API with real-time streaming',
    enabled: false,
  },
  {
    id: 'custom',
    name: 'Custom Broker',
    badge: '🔧 Bring Your Own',
    tagline: 'Connect any REST API endpoint',
    assets: 'Any asset class supported by your endpoint',
    why: ['Use your own broker or proprietary execution venue', 'Flexible REST integration'],
    api: 'Custom REST endpoint',
    enabled: false,
  },
];

export default function Settings() {
  const [user, setUser] = useState(null);
  const [brokerStatus, setBrokerStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [showKey, setShowKey] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [testingTelegram, setTestingTelegram] = useState(false);
  const [telegramTestResult, setTelegramTestResult] = useState(null);
  const [form, setForm] = useState({
    broker: '',
    broker_api_key: '',
    broker_api_secret: '',
    broker_mode: 'paper',
    trade_profile: 'balanced',
    promotion_mode: 'automatic',
    telegram_chat_id: '',
    telegram_notifications_enabled: false,
    auto_promote_enabled: false,
    auto_promote_min_trades: 20,
    auto_promote_min_win_rate: 60,
  });

  const loadUser = useCallback(async () => {
    try {
      const me = await base44.auth.me();
      setUser(me);
      // Load broker status from the secure backend function (no secrets returned)
      const status = await base44.functions.invoke('getBrokerStatus', {});
      setBrokerStatus(status);
      setForm({
        broker: status?.broker || me.broker || '',
        broker_api_key: '',
        broker_api_secret: '',
        broker_mode: status?.broker_mode || me.broker_mode || 'paper',
        trade_profile: me.trade_profile || 'balanced',
        promotion_mode: me.promotion_mode || 'automatic',
        telegram_chat_id: me.telegram_chat_id || '',
        telegram_notifications_enabled: me.telegram_notifications_enabled || false,
        auto_promote_enabled: me.auto_promote_enabled || false,
        auto_promote_min_trades: me.auto_promote_min_trades ?? 20,
        auto_promote_min_win_rate: me.auto_promote_min_win_rate ?? 60,
      });
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadUser();
  }, [loadUser]);

  const save = async () => {
    setSaving(true);
    try {
      // Save trade profile and promotion mode via auth.updateMe (non-sensitive)
      await base44.auth.updateMe({
        trade_profile: form.trade_profile,
        promotion_mode: form.promotion_mode,
        telegram_chat_id: form.telegram_chat_id,
        telegram_notifications_enabled: form.telegram_notifications_enabled,
        auto_promote_enabled: form.auto_promote_enabled,
        auto_promote_min_trades: Number(form.auto_promote_min_trades) || 20,
        auto_promote_min_win_rate: Number(form.auto_promote_min_win_rate) || 60,
      });

      // Save broker credentials via the secure backend function
      // (validates against the broker, stores in BrokerCredential entity, never on User)
      if (form.broker && form.broker_api_key.trim() && form.broker_api_secret.trim()) {
        const result = await base44.functions.invoke('saveBrokerCredentials', {
          broker: form.broker,
          api_key: form.broker_api_key.trim(),
          api_secret: form.broker_api_secret.trim(),
          mode: form.broker_mode,
        });
        if (result.error) {
          setTestResult({ ok: false, message: result.error });
        } else {
          setTestResult({ ok: true, message: `Credentials validated and saved for ${BROKERS.find((b) => b.id === form.broker)?.name || form.broker}.` });
        }
      }
      await loadUser();
    } catch (e) {
      setTestResult({ ok: false, message: e.message || 'Failed to save credentials.' });
    }
    setSaving(false);
  };

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await base44.functions.invoke('validateBrokerCredentials', {
        broker: form.broker,
        api_key: form.broker_api_key.trim(),
        api_secret: form.broker_api_secret.trim(),
        mode: form.broker_mode,
      });
      setTestResult(result);
    } catch (e) {
      setTestResult({ ok: false, message: 'Connection test failed.' });
    }
    setTesting(false);
  };

  const disconnect = async () => {
    setSaving(true);
    setTestResult(null);
    try {
      await base44.functions.invoke('saveBrokerCredentials', { disconnect: true });
      setForm((prev) => ({ ...prev, broker: '', broker_api_key: '', broker_api_secret: '', broker_mode: 'paper' }));
      await loadUser();
    } catch (e) {
      console.error(e);
    }
    setSaving(false);
  };

  const testTelegram = async () => {
    setTestingTelegram(true);
    setTelegramTestResult(null);
    try {
      // Save first so the chat ID is persisted before testing
      await base44.auth.updateMe({
        telegram_chat_id: form.telegram_chat_id,
        telegram_notifications_enabled: form.telegram_notifications_enabled,
      });
      const result = await base44.functions.invoke('sendTelegramAlert', {
        message: '🧪 TradePulse test notification — Telegram alerts are working!',
      });
      setTelegramTestResult(result.data || result);
    } catch (e) {
      setTelegramTestResult({ ok: false, error: e.message || 'Failed to send test message.' });
    }
    setTestingTelegram(false);
  };

  const isConnected = !!brokerStatus?.connected;
  const secretSuffix = brokerStatus?.credential_suffix || '';

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="p-4 md:p-8 pb-24 md:pb-8 max-w-3xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight flex items-center gap-2">
          <SettingsIcon className="w-7 h-7 text-primary" />
          Settings
        </h1>
        <p className="text-muted-foreground text-sm mt-1">
          Connect your broker and manage API credentials
        </p>
      </div>

      {/* Current trading mode banner */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className={cn(
          'rounded-2xl border p-5 mb-6 flex items-start gap-3',
          isConnected
            ? 'border-primary/30 bg-primary/5'
            : 'border-amber-500/30 bg-amber-500/5'
        )}
      >
        {isConnected ? (
          <ShieldCheck className="w-5 h-5 text-primary flex-shrink-0 mt-0.5" />
        ) : (
          <AlertTriangle className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" />
        )}
        <div>
          <h3 className="font-semibold text-sm mb-1">
            {isConnected ? 'Broker Connected' : 'Paper Trading Mode (No Broker Connected)'}
          </h3>
          <p className="text-xs text-muted-foreground leading-relaxed">
            {isConnected
              ? `Connected to ${BROKERS.find((b) => b.id === brokerStatus?.broker)?.name || brokerStatus?.broker} in ${brokerStatus?.broker_mode === 'live' ? 'LIVE' : 'paper'} mode. Orders are submitted to your broker and fills settle your portfolio automatically.`
              : 'Right now all trades are paper — recorded in the app with no real broker orders. Connect a broker below to route orders through your brokerage account.'}
          </p>
        </div>
      </motion.div>

      {/* Trading Profile */}
      <div className="mb-6">
        <TradingProfileSelector
          value={form.trade_profile}
          onChange={(id) => setForm({ ...form, trade_profile: id })}
        />
      </div>

      {/* Model Governance Promotion Mode */}
      <div className="mb-6">
        <div className="rounded-2xl border border-border bg-card p-5 space-y-4">
          <div className="flex items-center gap-2">
            <GitBranch className="w-4 h-4 text-accent" />
            <h2 className="font-semibold">Model Governance Promotion Mode</h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Controls how the AI's self-learning factor weights are promoted from candidate to champion.
            The governance cycle always runs offline (weekly) — it never interferes with live execution.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {[
              { id: 'research', label: 'Research', icon: FlaskConical, desc: 'Validate candidates only — never promote. Safest while proving the system.' },
              { id: 'manual_approval', label: 'Manual Approval', icon: UserCheck, desc: 'Generate and validate candidates. You approve each promotion by hand.' },
              { id: 'automatic', label: 'Automatic', icon: Zap, desc: 'Auto-promote candidates that pass all statistical gates. Full self-evolution.' },
            ].map((m) => (
              <button
                key={m.id}
                onClick={() => setForm({ ...form, promotion_mode: m.id })}
                className={cn(
                  'text-left rounded-xl border p-3 transition-all',
                  form.promotion_mode === m.id
                    ? 'border-primary bg-primary/10'
                    : 'border-border hover:border-primary/40'
                )}
              >
                <div className="flex items-center gap-2 mb-1">
                  <m.icon className="w-4 h-4 text-primary" />
                  <div className="font-medium text-sm">{m.label}</div>
                </div>
                <div className="text-xs text-muted-foreground leading-relaxed">{m.desc}</div>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Auto-Promote Paper → Live */}
      <div className="mb-6">
        <div className="rounded-2xl border border-border bg-card p-5 space-y-4">
          <div className="flex items-center gap-2">
            <Rocket className="w-4 h-4 text-primary" />
            <h2 className="font-semibold">Auto-Promote Paper → Live</h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Automatically switch from paper to live trading once your paper performance proves itself.
            The system counts closed positions (sells with a realized P&L) and promotes only when both
            the minimum trade count <em>and</em> minimum win rate are met. Fires once — it never re-triggers.
          </p>
          <div className="flex items-center justify-between">
            <div>
              <div className="font-medium text-sm">Enable Auto-Promotion</div>
              <div className="text-xs text-muted-foreground">Switch to live when thresholds are met</div>
            </div>
            <button
              type="button"
              onClick={() => setForm({ ...form, auto_promote_enabled: !form.auto_promote_enabled })}
              className={cn(
                'relative w-11 h-6 rounded-full transition-colors',
                form.auto_promote_enabled ? 'bg-primary' : 'bg-muted'
              )}
            >
              <span className={cn(
                'absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform',
                form.auto_promote_enabled && 'translate-x-5'
              )} />
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="mb-1.5 block">Min Closed Trades</Label>
              <Input
                type="number"
                min="1"
                value={form.auto_promote_min_trades}
                onChange={(e) => setForm({ ...form, auto_promote_min_trades: e.target.value })}
                className="font-mono"
              />
            </div>
            <div>
              <Label className="mb-1.5 block">Min Win Rate (%)</Label>
              <Input
                type="number"
                min="0"
                max="100"
                value={form.auto_promote_min_win_rate}
                onChange={(e) => setForm({ ...form, auto_promote_min_win_rate: e.target.value })}
                className="font-mono"
              />
            </div>
          </div>
          {user?.auto_promote_triggered_at && (
            <div className="rounded-lg p-3 text-sm flex items-start gap-2 bg-emerald-500/10 text-emerald-500">
              <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
              <span className="text-xs leading-relaxed">
                Auto-promotion fired on {new Date(user.auto_promote_triggered_at).toLocaleString('en-US', {
                  month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit',
                })}. Your account is now in LIVE mode.
              </span>
            </div>
          )}
          {form.auto_promote_enabled && !user?.auto_promote_triggered_at && (
            <p className="text-xs text-amber-400 flex items-center gap-1.5">
              <AlertTriangle className="w-3 h-3" />
              When both thresholds are met, your broker credentials will switch to LIVE and real orders will be placed.
            </p>
          )}
        </div>
      </div>

      {/* Telegram Notifications */}
      <div className="mb-6">
        <div className="rounded-2xl border border-border bg-card p-5 space-y-4">
          <div className="flex items-center gap-2">
            <Send className="w-4 h-4 text-primary" />
            <h2 className="font-semibold">Telegram Trade Notifications</h2>
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            Get instant Telegram messages when the autonomous AI executes trades. Create a bot via
            <span className="text-primary"> @BotFather </span>
            on Telegram, start a chat with your bot, then enter your chat ID below.
          </p>
          <div>
            <Label className="mb-1.5 block">Telegram Chat ID</Label>
            <Input
              value={form.telegram_chat_id}
              onChange={(e) => setForm({ ...form, telegram_chat_id: e.target.value })}
              placeholder="e.g. 123456789"
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground mt-1.5">
              Tip: message your bot, then open{' '}
              <span className="font-mono text-foreground/70">api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</span>{' '}
              to find your chat ID.
            </p>
          </div>
          <div className="flex items-center justify-between">
            <div>
              <div className="font-medium text-sm">Enable Telegram Alerts</div>
              <div className="text-xs text-muted-foreground">Send a message when trades execute</div>
            </div>
            <button
              type="button"
              onClick={() => setForm({ ...form, telegram_notifications_enabled: !form.telegram_notifications_enabled })}
              className={cn(
                'relative w-11 h-6 rounded-full transition-colors',
                form.telegram_notifications_enabled ? 'bg-primary' : 'bg-muted'
              )}
            >
              <span className={cn(
                'absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform',
                form.telegram_notifications_enabled && 'translate-x-5'
              )} />
            </button>
          </div>
          {telegramTestResult && (
            <div
              className={cn(
                'rounded-lg p-3 text-sm flex items-start gap-2',
                telegramTestResult.ok
                  ? 'bg-emerald-500/10 text-emerald-500'
                  : 'bg-red-500/10 text-red-500'
              )}
            >
              {telegramTestResult.ok ? (
                <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
              ) : (
                <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              )}
              <span className="text-xs leading-relaxed">
                {telegramTestResult.ok
                  ? 'Test message sent to your Telegram.'
                  : telegramTestResult.error || 'Failed to send test message.'}
              </span>
            </div>
          )}
          <Button
            onClick={testTelegram}
            disabled={testingTelegram || !form.telegram_chat_id}
            variant="secondary"
            className="gap-2 w-full"
          >
            {testingTelegram ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            Send Test Message
          </Button>
        </div>
      </div>

      {/* Broker connection card */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="rounded-2xl border border-border bg-card p-6 space-y-5"
      >
        <div className="flex items-center gap-2 mb-1">
          <Link2 className="w-4 h-4 text-accent" />
          <h2 className="font-semibold">Broker Connection</h2>
        </div>

        {/* Broker selector */}
        <div>
          <Label className="mb-2 block">Broker Platform</Label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {BROKERS.map((b) => (
              <button
                key={b.id}
                disabled={!b.enabled}
                onClick={() => b.enabled && setForm({ ...form, broker: b.id })}
                className={cn(
                  'text-left rounded-xl border p-3 transition-all',
                  !b.enabled && 'opacity-50 cursor-not-allowed',
                  form.broker === b.id && b.enabled
                    ? 'border-primary bg-primary/10'
                    : 'border-border hover:border-primary/40'
                )}
              >
                <div className="font-medium text-sm flex items-center gap-2">
                  {b.name}
                  {!b.enabled && (
                    <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                      Coming soon
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground mt-0.5">{b.tagline}</div>
              </button>
            ))}
          </div>

          {/* Selected broker details */}
          {form.broker &&
            (() => {
              const b = BROKERS.find((x) => x.id === form.broker);
              if (!b) return null;
              return (
                <div className="mt-3 rounded-xl border border-accent/20 bg-accent/5 p-4 space-y-3">
                  <div>
                    <div className="text-xs font-semibold text-accent">{b.badge}</div>
                    <div className="text-sm font-medium mt-0.5">{b.name}</div>
                    <div className="text-xs text-muted-foreground mt-0.5">{b.tagline}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                      Supported Assets
                    </div>
                    <p className="text-xs leading-relaxed">{b.assets}</p>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                      Why it fits the Superior Trader
                    </div>
                    <ul className="space-y-1">
                      {b.why.map((w, i) => (
                        <li key={i} className="text-xs leading-relaxed flex gap-1.5">
                          <CheckCircle2 className="w-3 h-3 text-primary flex-shrink-0 mt-0.5" />
                          <span>{w}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                      API Access
                    </div>
                    <p className="text-xs leading-relaxed">{b.api}</p>
                  </div>
                </div>
              );
            })()}
        </div>

        {/* API Key */}
        <div>
          <Label className="mb-1.5 block">API Key</Label>
          <div className="relative">
            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type={showKey ? 'text' : 'password'}
              value={form.broker_api_key}
              onChange={(e) => setForm({ ...form, broker_api_key: e.target.value })}
              placeholder={isConnected ? '•••••••• (enter new key to replace)' : 'Enter your API key'}
              className="pl-9 pr-10 font-mono"
            />
            <button
              type="button"
              onClick={() => setShowKey(!showKey)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* API Secret */}
        <div>
          <Label className="mb-1.5 block">API Secret</Label>
          {secretSuffix && (
            <p className="text-xs text-muted-foreground mb-1.5">
              Current secret: <span className="font-mono">{secretSuffix}</span> — leave blank to keep it.
            </p>
          )}
          <div className="relative">
            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type={showSecret ? 'text' : 'password'}
              value={form.broker_api_secret}
              onChange={(e) => setForm({ ...form, broker_api_secret: e.target.value })}
              placeholder={secretSuffix ? 'Enter new secret to replace' : 'Enter your API secret'}
              className="pl-9 pr-10 font-mono"
            />
            <button
              type="button"
              onClick={() => setShowSecret(!showSecret)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* Trading mode */}
        <div>
          <Label className="mb-2 block">Trading Mode</Label>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => setForm({ ...form, broker_mode: 'paper' })}
              className={cn(
                'rounded-xl border p-3 flex items-center gap-2 transition-all',
                form.broker_mode === 'paper'
                  ? 'border-primary bg-primary/10'
                  : 'border-border hover:border-primary/40'
              )}
            >
              <Shield className="w-4 h-4 text-muted-foreground" />
              <div>
                <div className="font-medium text-sm">Paper</div>
                <div className="text-xs text-muted-foreground">Simulation only</div>
              </div>
            </button>
            <button
              onClick={() => setForm({ ...form, broker_mode: 'live' })}
              className={cn(
                'rounded-xl border p-3 flex items-center gap-2 transition-all',
                form.broker_mode === 'live'
                  ? 'border-destructive bg-destructive/10'
                  : 'border-border hover:border-destructive/40'
              )}
            >
              <Rocket className="w-4 h-4 text-muted-foreground" />
              <div>
                <div className="font-medium text-sm">Live</div>
                <div className="text-xs text-muted-foreground">Real orders</div>
              </div>
            </button>
          </div>
          {form.broker_mode === 'live' && (
            <p className="text-xs text-amber-400 mt-2 flex items-center gap-1.5">
              <AlertTriangle className="w-3 h-3" />
              Live mode places real orders with real money. Use with caution.
            </p>
          )}
        </div>

        {/* Test result */}
        {testResult && (
          <div
            className={cn(
              'rounded-lg p-3 text-sm flex items-start gap-2',
              testResult.ok
                ? 'bg-emerald-500/10 text-emerald-500'
                : 'bg-red-500/10 text-red-500'
            )}
          >
            {testResult.ok ? (
              <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
            )}
            <span className="text-xs leading-relaxed">{testResult.message}</span>
          </div>
        )}

        {/* Actions */}
        <div className="flex flex-col sm:flex-row gap-2 pt-2">
          <Button onClick={save} disabled={saving} className="gap-2 flex-1">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Save Credentials
          </Button>
          <Button
            onClick={testConnection}
            disabled={testing || !form.broker || !form.broker_api_key.trim() || !form.broker_api_secret.trim()}
            variant="secondary"
            className="gap-2 flex-1"
          >
            {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Link2 className="w-4 h-4" />}
            Test Connection
          </Button>
          {isConnected && (
            <Button
              onClick={disconnect}
              disabled={saving}
              variant="outline"
              className="gap-2 text-red-500 hover:text-red-400"
            >
              <Unlink className="w-4 h-4" />
              Disconnect
            </Button>
          )}
        </div>
      </motion.div>

      {/* Security note */}
      <div className="mt-4 flex items-start gap-2 text-xs text-muted-foreground px-1">
        <Shield className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
        <p>
          Your API credentials are stored in an encrypted server-side vault and never exposed
          to the browser. The connection test validates your keys against the broker's live API.
          Never share your API secret. We recommend using paper trading keys until you're confident
          in the system.
        </p>
      </div>
    </div>
  );
}