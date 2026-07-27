import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Zap,
  Loader2,
  Brain,
  CheckCircle2,
  TrendingUp,
  TrendingDown,
  History,
  Sparkles,
  ArrowDownRight,
  ArrowUpRight,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import RecommendationBadge from '@/components/RecommendationBadge';
import { cn } from '@/lib/utils';

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n || 0);
}

function timeAgo(date) {
  if (!date) return '';
  const diff = Date.now() - new Date(date).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function AutonomousTrader() {
  const [scanning, setScanning] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [marketSummary, setMarketSummary] = useState('');
  const [proposals, setProposals] = useState([]);
  const [holdings, setHoldings] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [executedIds, setExecutedIds] = useState(new Set());

  const loadData = useCallback(async () => {
    try {
      const [h, d] = await Promise.all([
        base44.entities.Holding.list(),
        base44.entities.AITradeDecision.list('-created_date', 20),
      ]);
      setHoldings(h || []);
      setDecisions(d || []);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const runScan = async () => {
    setScanning(true);
    setProposals([]);
    setMarketSummary('');
    setExecutedIds(new Set());
    try {
      const portfolioContext =
        holdings.length > 0
          ? holdings
              .map(
                (h) =>
                  `${h.symbol} (${h.shares} shares, avg $${h.avg_price}, current $${h.current_price || h.avg_price})`
              )
              .join(', ')
          : 'No current positions';

      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `You are AlphaTrade AI in fully autonomous mode. Scan today's real-time stock market for the best trading opportunities.

Current portfolio: ${portfolioContext}

Identify 3-5 high-conviction trades to execute right now. Consider:
- Today's market trends and momentum
- Strong fundamental + technical setups
- Diversification across sectors
- Risk management — sensible position sizing, avoid over-concentration
- For SELL actions, only suggest selling stocks that are in the current portfolio

For each trade, provide a clear action (buy or sell), appropriate share count, confidence score (0-100), price target, and detailed reasoning grounded in real-time data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
            market_summary: {
              type: 'string',
              description: "Brief overview of today's market conditions and sentiment",
            },
            proposals: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  symbol: { type: 'string' },
                  company_name: { type: 'string' },
                  action: { type: 'string', enum: ['buy', 'sell'] },
                  shares: { type: 'number' },
                  current_price: { type: 'number' },
                  confidence: { type: 'number' },
                  target_price: { type: 'number' },
                  recommendation: {
                    type: 'string',
                    enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
                  },
                  reasoning: { type: 'string' },
                },
              },
            },
          },
          required: ['market_summary', 'proposals'],
        },
      });
      setMarketSummary(result.market_summary || '');
      setProposals(result.proposals || []);
    } catch (e) {
      console.error(e);
    }
    setScanning(false);
  };

  const executeProposal = async (proposal, index) => {
    const trade = {
      symbol: proposal.symbol,
      company_name: proposal.company_name || proposal.symbol,
      action: proposal.action,
      shares: proposal.shares,
      price: proposal.current_price,
      total_value: proposal.shares * proposal.current_price,
      ai_recommended: true,
    };
    await base44.entities.Trade.create(trade);

    if (proposal.action === 'buy') {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      if (existing) {
        const totalShares = existing.shares + proposal.shares;
        const totalCost =
          existing.shares * existing.avg_price + proposal.shares * proposal.current_price;
        await base44.entities.Holding.update(existing.id, {
          shares: totalShares,
          avg_price: totalCost / totalShares,
          current_price: proposal.current_price,
        });
      } else {
        await base44.entities.Holding.create({
          symbol: proposal.symbol,
          company_name: proposal.company_name || proposal.symbol,
          shares: proposal.shares,
          avg_price: proposal.current_price,
          current_price: proposal.current_price,
          sector: '',
          day_change_percent: 0,
        });
      }
    } else if (proposal.action === 'sell') {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      if (existing) {
        const newShares = existing.shares - proposal.shares;
        if (newShares <= 0) {
          await base44.entities.Holding.delete(existing.id);
        } else {
          await base44.entities.Holding.update(existing.id, { shares: newShares });
        }
      }
    }

    await base44.entities.AITradeDecision.create({
      symbol: proposal.symbol,
      company_name: proposal.company_name || proposal.symbol,
      action: proposal.action,
      shares: proposal.shares,
      price: proposal.current_price,
      confidence: proposal.confidence,
      target_price: proposal.target_price,
      reasoning: proposal.reasoning,
      status: 'executed',
    });

    setExecutedIds((prev) => new Set([...prev, index]));
  };

  const executeAll = async () => {
    setExecuting(true);
    for (let i = 0; i < proposals.length; i++) {
      if (!executedIds.has(i)) {
        await executeProposal(proposals[i], i);
      }
    }
    await loadData();
    setExecuting(false);
  };

  const pendingCount = proposals.filter((_, i) => !executedIds.has(i)).length;

  return (
    <div className="p-4 md:p-8 pb-24 md:pb-8 max-w-7xl mx-auto">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">
        <div>
          <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight flex items-center gap-2">
            Autonomous Trader
          </h1>
          <p className="text-muted-foreground text-sm mt-1">
            AI scans the market and executes trades on its own
          </p>
        </div>
        <div className="flex gap-2">
          {proposals.length > 0 && pendingCount > 0 && (
            <Button
              onClick={executeAll}
              disabled={executing}
              className="gap-2 bg-gradient-to-r from-primary to-accent"
            >
              {executing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
              Execute All ({pendingCount})
            </Button>
          )}
          <Button
            onClick={runScan}
            disabled={scanning}
            variant={scanning ? 'secondary' : 'default'}
            className="gap-2"
          >
            {scanning ? <Loader2 className="w-4 h-4 animate-spin" /> : <Brain className="w-4 h-4" />}
            {scanning ? 'Scanning Market...' : 'Run Market Scan'}
          </Button>
        </div>
      </div>

      {/* Scanning state */}
      {scanning && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-accent/30 bg-gradient-to-br from-accent/10 to-primary/5 p-8 md:p-12 text-center"
        >
          <div className="relative w-16 h-16 mx-auto mb-4">
            <motion.div
              animate={{ scale: [1, 1.1, 1], opacity: [0.7, 1, 0.7] }}
              transition={{ duration: 2, repeat: Infinity }}
              className="w-16 h-16 rounded-2xl bg-gradient-to-br from-accent to-primary flex items-center justify-center glow-accent"
            >
              <Brain className="w-8 h-8 text-white" />
            </motion.div>
          </div>
          <h3 className="text-lg font-semibold mb-1">Scanning the market</h3>
          <p className="text-muted-foreground text-sm">
            Analyzing real-time prices, fundamentals, and sentiment across sectors...
          </p>
        </motion.div>
      )}

      {/* Market summary */}
      {!scanning && marketSummary && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-border bg-card p-5 mb-6"
        >
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="w-4 h-4 text-accent" />
            <h3 className="font-semibold text-sm">AI Market Summary</h3>
          </div>
          <p className="text-sm text-muted-foreground leading-relaxed">{marketSummary}</p>
        </motion.div>
      )}

      {/* Proposals */}
      {!scanning && proposals.length > 0 && (
        <div className="space-y-4 mb-8">
          <AnimatePresence>
            {proposals.map((p, i) => {
              const isExecuted = executedIds.has(i);
              const canSell = p.action !== 'sell' || holdings.some((h) => h.symbol === p.symbol);
              return (
                <motion.div
                  key={i}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.08 }}
                  className={cn(
                    'rounded-2xl border bg-card p-5 transition-colors',
                    isExecuted ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border'
                  )}
                >
                  <div className="flex items-start justify-between flex-wrap gap-3 mb-3">
                    <div className="flex items-center gap-3">
                      <div
                        className={cn(
                          'w-10 h-10 rounded-full flex items-center justify-center',
                          p.action === 'buy'
                            ? 'bg-emerald-500/10 text-emerald-500'
                            : 'bg-red-500/10 text-red-500'
                        )}
                      >
                        {p.action === 'buy' ? (
                          <ArrowDownRight className="w-5 h-5" />
                        ) : (
                          <ArrowUpRight className="w-5 h-5" />
                        )}
                      </div>
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-bold text-base">{p.symbol}</span>
                          <span className="text-xs text-muted-foreground">{p.company_name}</span>
                        </div>
                        <div className="text-sm text-muted-foreground mt-0.5">
                          {p.action === 'buy' ? 'Buy' : 'Sell'} {p.shares} shares @{' '}
                          {formatCurrency(p.current_price)}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <RecommendationBadge recommendation={p.recommendation} />
                      {isExecuted && (
                        <span className="flex items-center gap-1 text-xs text-emerald-500 font-medium">
                          <CheckCircle2 className="w-4 h-4" /> Executed
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-3 text-sm mb-3">
                    <div>
                      <div className="text-xs text-muted-foreground">Confidence</div>
                      <div className="font-semibold">{p.confidence}%</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Target</div>
                      <div className="font-semibold text-emerald-500">
                        {formatCurrency(p.target_price)}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Total</div>
                      <div className="font-semibold">
                        {formatCurrency(p.shares * p.current_price)}
                      </div>
                    </div>
                  </div>

                  {p.reasoning && (
                    <p className="text-sm text-muted-foreground leading-relaxed mb-3">
                      {p.reasoning}
                    </p>
                  )}

                  {!isExecuted && (
                    <Button
                      size="sm"
                      onClick={() => executeProposal(p, i)}
                      disabled={executing || !canSell}
                      variant="secondary"
                      className="gap-1.5"
                    >
                      <Zap className="w-3.5 h-3.5" />
                      {canSell ? 'Execute Trade' : 'Cannot sell — not held'}
                    </Button>
                  )}
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}

      {/* Empty state */}
      {!scanning && proposals.length === 0 && decisions.length === 0 && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-border bg-card p-12 text-center"
        >
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-accent to-primary flex items-center justify-center mx-auto mb-4 glow-accent">
            <Brain className="w-8 h-8 text-white" />
          </div>
          <h3 className="text-lg font-semibold mb-2">Autonomous mode is ready</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            Click "Run Market Scan" and the AI will analyze the live market, identify the best
            opportunities, and propose trades you can execute in one click.
          </p>
          <Button onClick={runScan} className="gap-2 bg-gradient-to-r from-primary to-accent">
            <Brain className="w-4 h-4" />
            Run First Scan
          </Button>
        </motion.div>
      )}

      {/* Decision history */}
      {decisions.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-border bg-card overflow-hidden"
        >
          <div className="p-5 border-b border-border flex items-center gap-2">
            <History className="w-4 h-4 text-muted-foreground" />
            <h3 className="font-semibold">Decision History</h3>
          </div>
          <div className="divide-y divide-border/50">
            {decisions.map((d) => (
              <div key={d.id} className="flex items-center justify-between p-4 gap-3">
                <div className="flex items-center gap-3 min-w-0">
                  <div
                    className={cn(
                      'w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0',
                      d.action === 'buy'
                        ? 'bg-emerald-500/10 text-emerald-500'
                        : 'bg-red-500/10 text-red-500'
                    )}
                  >
                    {d.action === 'buy' ? (
                      <TrendingUp className="w-4 h-4" />
                    ) : (
                      <TrendingDown className="w-4 h-4" />
                    )}
                  </div>
                  <div className="min-w-0">
                    <div className="font-medium text-sm">
                      {d.action.toUpperCase()} {d.symbol}
                    </div>
                    <div className="text-xs text-muted-foreground truncate max-w-[300px]">
                      {d.shares} @ {formatCurrency(d.price)} · {d.confidence}% confidence
                    </div>
                  </div>
                </div>
                <div className="text-right flex-shrink-0">
                  <div className="text-xs text-muted-foreground">{timeAgo(d.created_date)}</div>
                  <span className="text-xs text-emerald-500">{d.status}</span>
                </div>
              </div>
            ))}
          </div>
        </motion.div>
      )}
    </div>
  );
}