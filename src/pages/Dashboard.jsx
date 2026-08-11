import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import {
  Wallet,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  PieChart as PieChartIcon,
  ArrowUpRight,
  ArrowDownRight,
  Loader2,
} from 'lucide-react';
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts';
import StatCard from '@/components/StatCard';
import SectorExposure from '@/components/SectorExposure';
import ExitAlerts from '@/components/ExitAlerts';
import RealDataPipeline from '@/components/RealDataPipeline';
import RealPnlChart from '@/components/RealPnlChart';
import PerformanceAttribution from '@/components/PerformanceAttribution';
import RiskAnalytics from '@/components/RiskAnalytics';
import BenchmarkComparison from '@/components/BenchmarkComparison';
import PortfolioOptimization from '@/components/PortfolioOptimization';
import BalanceVerification from '@/components/BalanceVerification';
import ScanRunStatus from '@/components/ScanRunStatus';
import AssetClassBreakdown from '@/components/AssetClassBreakdown';
import TradingSessionControl from '@/components/TradingSessionControl';
import { Button } from '@/components/ui/button';

const COLORS = ['#10b981', '#8b5cf6', '#3b82f6', '#f59e0b', '#ec4899', '#06b6d4', '#84cc16', '#f97316'];

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

function functionErrorMessage(error, fallback) {
  return error?.response?.data?.error || error?.data?.error || error?.message || fallback;
}

export default function Dashboard() {
  const [holdings, setHoldings] = useState([]);
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [analyticsTrigger, setAnalyticsTrigger] = useState(0);
  const [dataError, setDataError] = useState(null);
  const [brokerAccount, setBrokerAccount] = useState(null);
  const [recentOrdersError, setRecentOrdersError] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const [h, t, me, snapshotResponse] = await Promise.all([
        base44.entities.Holding.list(),
        base44.entities.Trade.list('-created_date', 10),
        base44.auth.me(),
        base44.functions.invoke('syncBrokerPositions', { snapshot_only: true }).catch((error) => ({
          brokerLoadError: functionErrorMessage(error, 'Unable to load Alpaca portfolio'),
        })),
      ]);
      const appHoldings = h || [];
      const brokerSnapshot = snapshotResponse?.data || snapshotResponse;
      const alpacaConnected = me?.broker === 'alpaca';
      if (brokerSnapshot?.positions) {
        const appBySymbol = new Map(
          appHoldings.map((holding) => [String(holding.symbol).toUpperCase(), holding])
        );
        setHoldings(brokerSnapshot.positions.map((position) => ({
          ...appBySymbol.get(position.symbol),
          ...position,
          id: appBySymbol.get(position.symbol)?.id || `alpaca-${position.asset_id || position.symbol}`,
          broker_authoritative: true,
        })));
        setBrokerAccount(brokerSnapshot.account || null);
      } else if (!alpacaConnected) {
        setHoldings(appHoldings);
        setBrokerAccount(null);
      } else {
        setHoldings([]);
        setBrokerAccount(null);
      }
      const localTrades = t || [];
      const brokerOrders = brokerSnapshot?.orders;
      if (brokerOrders) {
        const localByOrderId = new Map(
          localTrades
            .filter((trade) => trade.broker_order_id)
            .map((trade) => [trade.broker_order_id, trade])
        );
        setTrades(brokerOrders.map((order) => ({
          ...localByOrderId.get(order.broker_order_id),
          ...order,
        })));
        setRecentOrdersError(brokerSnapshot?.orders_error || null);
      } else {
        setTrades(alpacaConnected ? [] : localTrades);
        setRecentOrdersError(alpacaConnected ? brokerSnapshot?.brokerLoadError || 'Unable to load Alpaca portfolio and orders' : null);
      }
      setDataError(
        alpacaConnected && !brokerSnapshot?.positions
          ? `Alpaca portfolio unavailable; local holdings are hidden. ${brokerSnapshot?.brokerLoadError || ''}`.trim()
          : brokerSnapshot?.account_error || null
      );
    } catch (e) {
      setDataError(e.message || 'Unable to load portfolio data');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
    // Poll every 45s so the Dashboard reflects scheduled scans and broker
    // reconciliation that happen while the page is open.
    const timer = setInterval(loadData, 45000);
    // Refresh when the tab becomes visible again (covers scheduled scans
    // that ran while the user was away).
    const onVisible = () => {
      if (document.visibilityState === 'visible') loadData();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [loadData]);

  // Fetches fresh holdings internally (not from closure state) so it works
  // correctly when called as a post-cycle completion callback, when the local
  // holdings state is still stale.
  const refreshPrices = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true);
    try {
      const [appHoldings, brokerResponse] = await Promise.all([
        base44.entities.Holding.list(),
        base44.functions.invoke('syncBrokerPositions', { snapshot_only: true }),
      ]);
      const brokerSnapshot = brokerResponse?.data || brokerResponse;
      const appBySymbol = new Map(
        (appHoldings || []).map((holding) => [String(holding.symbol).toUpperCase(), holding])
      );
      const fresh = brokerSnapshot?.positions
        ? brokerSnapshot.positions.map((position) => ({
            ...appBySymbol.get(position.symbol),
            ...position,
            id: appBySymbol.get(position.symbol)?.id || `alpaca-${position.asset_id || position.symbol}`,
            broker_authoritative: true,
          }))
        : appHoldings;
      if (brokerSnapshot?.account) setBrokerAccount(brokerSnapshot.account);
      if (!fresh || fresh.length === 0) {
        setHoldings([]);
        setRefreshing(false);
        return;
      }
      const items = fresh.map((h) => ({ symbol: h.symbol, asset_class: h.asset_class || 'stocks' }));
      const result = await base44.functions.invoke('getMultiAssetQuotes', { items });
      const quotes = (result.data?.quotes || result.quotes || []).filter((q) => !q.error);
      const quoteMap = quotes.reduce((acc, q) => {
        acc[q.symbol.toUpperCase()] = q;
        return acc;
      }, {});
      setHoldings(fresh.map((holding) => {
        const quote = quoteMap[String(holding.symbol).toUpperCase()];
        if (!quote) return holding;
        return holding.broker_authoritative
          ? { ...holding, day_change_percent: quote.day_change_percent }
          : { ...holding, current_price: quote.price, day_change_percent: quote.day_change_percent };
      }));
      setDataError(null);
    } catch (e) {
      setDataError(e.message || 'Unable to refresh prices');
    }
    if (!silent) setRefreshing(false);
  }, []);

  // Auto-refresh display prices every 60s (silent — no spinner flash). The
  // browser never persists Holding projections; settlement remains the writer.
  useEffect(() => {
    const priceTimer = setInterval(() => { refreshPrices(true); }, 60000);
    return () => clearInterval(priceTimer);
  }, [refreshPrices]);

  // Real-time subscription — patches holdings instantly on create/update/delete
  // (e.g. when a scan executes a trade or reconciliation adjusts a position),
  // without waiting for the next poll.
  useEffect(() => {
    const unsubscribe = base44.entities.Holding.subscribe((event) => {
      if (brokerAccount) return;
      setHoldings((prev) => {
        if (event.type === 'create') {
          return prev.some((h) => h.id === event.data.id) ? prev : [...prev, event.data];
        }
        if (event.type === 'update') {
          return prev.map((h) => (h.id === event.data.id ? { ...h, ...event.data } : h));
        }
        if (event.type === 'delete') {
          return prev.filter((h) => h.id !== event.data.id);
        }
        return prev;
      });
    });
    return unsubscribe;
  }, [brokerAccount]);

  // Called after a Start Trader cycle completes: reload holdings/trades, then
  // refresh all holding prices so portfolio totals reflect the new state.
  const handleCycleComplete = async () => {
    await loadData();
    await refreshPrices();
    setAnalyticsTrigger((n) => n + 1);
  };

  const handleStart = () => setAnalyticsTrigger((n) => n + 1);

  const handleExitPosition = async (holding) => {
    const exitPrice = holding.current_price || holding.avg_price;
    try {
      await base44.functions.invoke('executeTrade', {
        symbol: holding.symbol,
        action: 'sell',
        qty: holding.shares,
        price: exitPrice,
        company_name: holding.company_name,
        asset_class: holding.asset_class || 'stocks',
        portfolio_id: holding.portfolio_id || null,
        ai_recommended: false,
        source: 'dashboard_exit',
        notes: 'Manual exit from Dashboard',
        idempotency_key: `dashboard-exit-${holding.id}-${holding.shares}`,
        signal_timestamp: new Date().toISOString(),
      });
      await loadData();
    } catch (e) {
      setDataError(e.message || 'Exit request failed');
    }
  };

  const openPositionValue = holdings.reduce(
    (sum, h) => sum + (h.broker_authoritative ? h.market_value : h.shares * (h.current_price || h.avg_price)),
    0
  );
  const openCostBasis = holdings.reduce((sum, h) => sum + h.shares * h.avg_price, 0);
  const unrealizedPL = holdings.reduce(
    (sum, h) => sum + (h.broker_authoritative ? h.unrealized_pl : h.shares * ((h.current_price || h.avg_price) - h.avg_price)),
    0
  );
  const unrealizedPLPercent = openCostBasis > 0 ? (unrealizedPL / openCostBasis) * 100 : 0;
  const openPositionsDayMove = holdings.reduce((sum, h) => {
    const current = h.current_price || h.avg_price;
    const pct = Number(h.day_change_percent) || 0;
    const previousClose = pct > -100 ? current / (1 + pct / 100) : current;
    return sum + h.shares * (current - previousClose);
  }, 0);
  const accountEquity = brokerAccount?.equity;

  const pieData = holdings.map((h) => ({
    name: h.symbol,
    value: h.broker_authoritative ? h.market_value : h.shares * (h.current_price || h.avg_price),
  }));

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="p-4 md:p-8 pb-24 md:pb-8 max-w-7xl mx-auto">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">
        <div>
          <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight">Portfolio</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Track positions and AI-driven insights
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button
            variant="secondary"
            onClick={refreshPrices}
            disabled={refreshing || holdings.length === 0}
            className="gap-2"
          >
            {refreshing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Refresh Prices
          </Button>
        </div>
      </div>

      {dataError && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 mb-6 text-sm text-red-500">
          {dataError}
        </div>
      )}

      <TradingSessionControl onComplete={handleCycleComplete} onStart={handleStart} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-8">
        <StatCard
          label={accountEquity != null ? 'Alpaca Account Equity' : 'Open Position Value'}
          value={formatCurrency(accountEquity ?? openPositionValue)}
          icon={Wallet}
          accent
        />
        <StatCard label="Open Cost Basis" value={formatCurrency(openCostBasis)} icon={TrendingUp} />
        <StatCard
          label="Unrealized P&L"
          value={formatCurrency(unrealizedPL)}
          change={unrealizedPLPercent}
          changeLabel="open positions"
          icon={unrealizedPL >= 0 ? TrendingUp : TrendingDown}
        />
        <StatCard label="Open Positions Day Move" value={formatCurrency(openPositionsDayMove)} icon={TrendingUp} />
      </div>

      <AssetClassBreakdown holdings={holdings} />

      <BalanceVerification holdings={holdings} onSynced={loadData} />

      <ScanRunStatus />

      <RealDataPipeline />

      <RealPnlChart />

      <PerformanceAttribution />

      <RiskAnalytics holdings={holdings} autoTriggerKey={analyticsTrigger} />

      <BenchmarkComparison holdings={holdings} autoTriggerKey={analyticsTrigger} />

      <PortfolioOptimization holdings={holdings} autoTriggerKey={analyticsTrigger} />

      <ExitAlerts holdings={holdings} onExit={handleExitPosition} />

      {holdings.length === 0 ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-border bg-card p-12 text-center"
        >
          <Wallet className="w-12 h-12 mx-auto text-muted-foreground mb-4" />
          <h3 className="text-lg font-semibold mb-2">No positions yet</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            Positions appear only after an authoritative fill is processed through settlement.
          </p>
        </motion.div>
      ) : (
        <>
          <div className="grid lg:grid-cols-3 gap-4 mb-8">
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-2xl border border-border bg-card p-5 lg:col-span-1"
            >
              <h3 className="font-semibold mb-4 flex items-center gap-2">
                <PieChartIcon className="w-4 h-4 text-accent" />
                Allocation
              </h3>
              <ResponsiveContainer width="100%" height={250}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={50}
                    outerRadius={80}
                    paddingAngle={2}
                  >
                    {pieData.map((_, i) => (
                      <Cell key={i} fill={COLORS[i % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      background: '#131826',
                      border: '1px solid #232b3d',
                      borderRadius: '8px',
                    }}
                    formatter={(v) => formatCurrency(v)}
                  />
                </PieChart>
              </ResponsiveContainer>
            </motion.div>
          </div>

          <SectorExposure holdings={holdings} />

          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-2xl border border-border bg-card overflow-hidden mb-8"
          >
            <div className="p-5 border-b border-border">
              <h3 className="font-semibold">Holdings</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-muted-foreground text-xs border-b border-border">
                    <th className="text-left font-medium p-4">Symbol</th>
                    <th className="text-right font-medium p-4">Shares</th>
                    <th className="text-right font-medium p-4 hidden md:table-cell">Avg Price</th>
                    <th className="text-right font-medium p-4">Current</th>
                    <th className="text-right font-medium p-4 hidden sm:table-cell">Day %</th>
                    <th className="text-right font-medium p-4">Value</th>
                    <th className="text-right font-medium p-4">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {holdings.map((h) => {
                    const value = h.broker_authoritative ? h.market_value : h.shares * (h.current_price || h.avg_price);
                    const pl = h.broker_authoritative
                      ? h.unrealized_pl
                      : h.shares * ((h.current_price || h.avg_price) - h.avg_price);
                    const plPercent = h.broker_authoritative
                      ? h.unrealized_pl_percent
                      : h.avg_price > 0 ? (pl / (h.shares * h.avg_price)) * 100 : 0;
                    return (
                      <tr
                        key={h.id}
                        className="border-b border-border/50 hover:bg-muted/30 transition-colors"
                      >
                        <td className="p-4">
                          <div className="font-semibold">{h.symbol}</div>
                          {h.company_name && (
                            <div className="text-xs text-muted-foreground hidden md:block truncate max-w-[140px]">
                              {h.company_name}
                            </div>
                          )}
                        </td>
                        <td className="text-right p-4">{h.shares}</td>
                        <td className="text-right p-4 hidden md:table-cell">
                          {formatCurrency(h.avg_price)}
                        </td>
                        <td className="text-right p-4 font-medium">
                          {formatCurrency(h.current_price || h.avg_price)}
                        </td>
                        <td className="text-right p-4 hidden sm:table-cell">
                          <span
                            className={
                              (h.day_change_percent || 0) >= 0 ? 'text-emerald-500' : 'text-red-500'
                            }
                          >
                            {(h.day_change_percent || 0) >= 0 ? '+' : ''}
                            {(h.day_change_percent || 0).toFixed(2)}%
                          </span>
                        </td>
                        <td className="text-right p-4 font-medium">{formatCurrency(value)}</td>
                        <td className="text-right p-4">
                          <span className={pl >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                            {pl >= 0 ? '+' : ''}
                            {formatCurrency(pl)}
                            <span className="text-xs block opacity-80">
                              ({plPercent >= 0 ? '+' : ''}
                              {plPercent.toFixed(2)}%)
                            </span>
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </motion.div>

          {(trades.length > 0 || brokerAccount) && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-2xl border border-border bg-card overflow-hidden"
            >
              <div className="p-5 border-b border-border">
                <h3 className="font-semibold">Recent Alpaca Orders</h3>
              </div>
              <div className="divide-y divide-border/50">
                {trades.length === 0 && !recentOrdersError && (
                  <div className="p-4 text-sm text-muted-foreground">
                    Alpaca reports no recent orders.
                  </div>
                )}
                {trades.map((t) => (
                  <div key={t.id} className="flex items-center justify-between p-4">
                    <div className="flex items-center gap-3">
                      <div
                        className={`w-9 h-9 rounded-full flex items-center justify-center ${
                          t.action === 'buy'
                            ? 'bg-emerald-500/10 text-emerald-500'
                            : 'bg-red-500/10 text-red-500'
                        }`}
                      >
                        {t.action === 'buy' ? (
                          <ArrowDownRight className="w-4 h-4" />
                        ) : (
                          <ArrowUpRight className="w-4 h-4" />
                        )}
                      </div>
                      <div>
                        <div className="font-medium text-sm">
                          {t.action.toUpperCase()} {t.symbol}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {t.filled_qty != null ? `${t.filled_qty}/${t.shares}` : t.shares} shares
                          {t.price != null ? ` @ ${formatCurrency(t.price)}` : ''}
                        </div>
                        {(t.filled_at || t.submitted_at || t.last_fill_at || t.first_fill_at || t.created_date) && (
                          <div className="text-xs text-muted-foreground/70 mt-0.5">
                            {new Date(t.filled_at || t.submitted_at || t.last_fill_at || t.first_fill_at || t.created_date).toLocaleString('en-US', {
                              month: 'short',
                              day: 'numeric',
                              year: 'numeric',
                              hour: 'numeric',
                              minute: '2-digit',
                            })}
                          </div>
                        )}
                        {t.broker_authoritative && (
                          <div className="text-xs text-muted-foreground/70 mt-0.5">
                            Alpaca {String(t.status || 'fill').replace(/_/g, ' ')}
                            {t.broker_order_id ? ` · Order ${t.broker_order_id}` : ''}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="font-medium text-sm">
                        {t.total_value != null ? formatCurrency(t.total_value) : 'Awaiting fill'}
                      </div>
                      {t.ai_recommended && (
                        <span className="text-xs text-accent">AI Suggested</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </motion.div>
          )}
          {recentOrdersError && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-500">
              Recent Alpaca orders unavailable: {recentOrdersError}
            </div>
          )}
        </>
      )}
    </div>
  );
}
