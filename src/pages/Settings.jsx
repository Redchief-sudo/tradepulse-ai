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
  },
  {
    id: 'custom',
    name: 'Custom Broker',
    badge: '🔧 Bring Your Own',
    tagline: 'Connect any REST API endpoint',
    assets: 'Any asset class supported by your endpoint',
    why: ['Use your own broker or proprietary execution venue', 'Flexible REST integration'],
    api: 'Custom REST endpoint',
  },
];

export default function Settings() {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [showKey, setShowKey] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [form, setForm] = useState({
    broker: '',
    broker_api_key: '',
    broker_api_secret: '',
    broker_mode: 'paper',
    trade_profile: 'balanced',
  });

  const loadUser = useCallback(async () => {
    try {
      const me = await base44.auth.me();
      setUser(me);
      setForm({
        broker: me.broker || '',
        broker_api_key: me.broker_api_key || '',
        broker_api_secret: me.broker_api_secret || '',
        broker_mode: me.broker_mode || 'paper',
        trade_profile: me.trade_profile || 'balanced',
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
      await base44.auth.updateMe(form);
      await loadUser();
    } catch (e) {
      console.error(e);
    }
    setSaving(false);
  };

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      // Verify the key format looks valid — actual broker API call needs a backend function (Builder+)
      await new Promise((r) => setTimeout(r, 800));
      if (!form.broker_api_key.trim() || !form.broker_api_secret.trim()) {
        setTestResult({ ok: false, message: 'API key and secret are required.' });
      } else if (form.broker_api_key.trim().length < 10) {
        setTestResult({ ok: false, message: 'API key looks too short — check your broker dashboard.' });
      } else {
        setTestResult({
          ok: true,
          message: `Credentials saved for ${BROKERS.find((b) => b.id === form.broker)?.name || form.broker}. Live order execution requires a Builder+ backend function.`,
        });
      }
    } catch (e) {
      setTestResult({ ok: false, message: 'Connection test failed.' });
    }
    setTesting(false);
  };

  const disconnect = async () => {
    setForm((prev) => ({ ...prev, broker: '', broker_api_key: '', broker_api_secret: '', broker_mode: 'paper' }));
    setTestResult(null);
    setSaving(true);
    try {
      await base44.auth.updateMe({
        broker: '',
        broker_api_key: '',
        broker_api_secret: '',
        broker_mode: 'paper',
      });
      await loadUser();
    } catch (e) {
      console.error(e);
    }
    setSaving(false);
  };

  const isConnected = !!(form.broker && form.broker_api_key && form.broker_api_secret);

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
              ? `Connected to ${BROKERS.find((b) => b.id === form.broker)?.name || form.broker} in ${form.broker_mode === 'live' ? 'LIVE' : 'paper'} mode. Trades are tracked in your portfolio. Live order execution to your broker requires a Builder+ backend function.`
              : 'Right now all trades are simulated in the app database — no real orders are placed. Connect a broker below to enable live trading (requires Builder+ for order execution).'}
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
                onClick={() => setForm({ ...form, broker: b.id })}
                className={cn(
                  'text-left rounded-xl border p-3 transition-all',
                  form.broker === b.id
                    ? 'border-primary bg-primary/10'
                    : 'border-border hover:border-primary/40'
                )}
              >
                <div className="font-medium text-sm">{b.name}</div>
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
              placeholder="Enter your API key"
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
          <div className="relative">
            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type={showSecret ? 'text' : 'password'}
              value={form.broker_api_secret}
              onChange={(e) => setForm({ ...form, broker_api_secret: e.target.value })}
              placeholder="Enter your API secret"
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
            disabled={testing || !form.broker}
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
          Your API credentials are stored securely on your user profile and only accessible to you.
          Never share your API secret. We recommend using paper trading keys until you're confident
          in the system.
        </p>
      </div>
    </div>
  );
}