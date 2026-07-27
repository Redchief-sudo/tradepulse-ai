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
  ChevronDown,
  ChevronUp,
  Shield,
  Activity,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import RecommendationBadge from '@/components/RecommendationBadge';
import TradePerformance from '@/components/TradePerformance';
import { runAutonomousScan } from '@/lib/autonomousScan';
import {
  computeSectorExposure,
  computePortfolioValue,
  computeCappedPositionSize,
  formatCurrency,
} from '@/lib/portfolio';
import { cn } from '@/lib/utils';

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
  const [scanStage, setScanStage] = useState('');
  const [executing, setExecuting] = useState(false);
  const [marketSummary, setMarketSummary] = useState('');
  const [riskAssessment, setRiskAssessment] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [proposals, setProposals] = useState([]);
  const [holdings, setHoldings] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [executedIds, setExecutedIds] = useState(new Set());
  const [showCandidates, setShowCandidates] = useState(false);

  const loadData = useCallback(async () => {
    try {
      const [h, d] = await Promise.all([
        base44.entities.Holding.list(),
        base44.entities.AITradeDecision.list('-created_date', 30),
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
    setCandidates([]);
    setMarketSummary('');
    setRiskAssessment('');
    setExecutedIds(new Set());
    try {
      setScanStage('Pass 1: Deep market scan');
      const result = await runAutonomousScan(holdings);
      setScanStage('Pass 2: Portfolio fit & risk');
      setMarketSummary(result.marketSummary);
      setCandidates(result.candidates);
      setRiskAssessment(result.riskAssessment);
      setProposals(result.proposals);
    } catch (e) {
      console.error(e);
    }
    setScanStage('');
    setScanning(false);
  };

  const executeProposal = async (proposal, index) => {
    const portfolioValue = computePortfolioValue(holdings);
    const sectorData = computeSectorExposure(holdings);
    const currentSectorValue =
      sectorData.sectors.find((s) => s.sector === (proposal.sector || 'Other'))?.value || 0;

    let shares, positionValue;
    if (proposal.action === 'buy') {
      const sized = computeCappedPositionSize(
        proposal.suggested_position_pct || 10,
        proposal.current_price,
        portfolioValue,
        currentSectorValue
      );
      shares = sized.shares;
      positionValue = sized.positionValue;
    } else {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      shares = existing ? Math.min(proposal.shares || existing.shares, existing.shares) : 0;
      positionValue = shares * proposal.current_price;
    }

    if (shares <= 0) return;

    const trade = {
      symbol: proposal.symbol,
      company_name: proposal.company_name || proposal.symbol,
      action: proposal.action,
      shares,
      price: proposal.current_price,
      total_value: positionValue,
      ai_recommended: true,
    };
    await base44.entities.Trade.create(trade);

    if (proposal.action === 'buy') {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      if (existing) {
        const totalShares = existing.shares + shares;
        const totalCost = existing.shares * existing.avg_price + positionValue;
        await base44.entities.Holding.update(existing.id, {
          shares: totalShares,
          avg_price: totalCost / totalShares,
          current_price: proposal.current_price,
          stop_loss: proposal.stop_loss,
          target_price: proposal.target_price,
        });
      } else {
        await base44.entities.Holding.create({
          symbol: proposal.symbol,
          company_name: proposal.company_name || proposal.symbol,
          shares,
          avg_price: proposal.current_price,
          current_price: proposal.current_price,
          sector: proposal.sector || '',
          day_change_percent: 0,
          stop_loss: proposal.stop_loss,
          target_price: proposal.target_price,
        });
      }
    } else {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      if (existing) {
        const newShares = existing.shares - shares;
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
      sector: proposal.sector || '',
      action: proposal.action,
      shares,
      price: proposal.current_price,
      position_value: positionValue,
      confidence: proposal.confidence,
      target_price: proposal.target_price,
      stop_loss: proposal.stop_loss,
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
          <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight">
            Autonomous Trader
          </h1>
          <p className="text-muted-foreground text-sm mt-1">
            Multi-pass AI analysis with risk-aware execution
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
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
            {scanning ? 'Scanning...' : 'Run Market Scan'}
          </Button>
        </div>
      </div>

      {/* Scanning state */}
      {scanning && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-accent/30 bg-gradient-to-br from-accent/10 to-primary/5 p-8 md:p-12 text-center mb-6"
        >
          <motion.div
            animate={{ scale: [1, 1.1, 1], opacity: [0.7, 1, 0.7] }}
            transition={{ duration: 2, repeat: Infinity }}
            className="w-16 h-16 rounded-2xl bg-gradient-to-br from-accent to-primary flex items-center justify-center mx-auto mb-4 glow-accent"
          >
            <Brain className="w-8 h-8 text-white" />
          </motion.div>
          <h3 className="text-lg font-semibold mb-1">{scanStage || 'Scanning...'}</h3>
          <p className="text-muted-foreground text-sm">
            Analyzing real-time prices, fundamentals, technicals, news, and portfolio fit...
          </p>
        </motion.div>
      )}

      {/* Market summary */}
      {!scanning && marketSummary && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-border bg-card p-5 mb-4"
        >
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="w-4 h-4 text-accent" />
            <h3 className="font-semibold text-sm">AI Market Summary</h3>
          </div>
          <p className="text-sm text-muted-foreground leading-relaxed">{marketSummary}</p>
        </motion.div>
      )}

      {/* Risk assessment */}
      {!scanning && riskAssessment && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="rounded-2xl border border-primary/20 bg-primary/5 p-5 mb-6"
        >
          <div className="flex items-center gap-2 mb-2">
            <Shield className="w-4 h-4 text-primary" />
            <h3 className="font-semibold text-sm">Risk Assessment & Rebalancing</h3>
          </div>
          <p className="text-sm text-muted-foreground leading-relaxed">{riskAssessment}</p>
        </motion.div>
      )}

      {/* Candidates deep scan (collapsible) */}
      {!scanning && candidates.length > 0 && (
        <div className="mb-6">
          <button
            onClick={() => setShowCandidates(!showCandidates)}
            className="flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors mb-3"
          >
            <Activity className="w-4 h-4" />
            Deep Scan: {candidates.length} Candidates Analyzed
            {showCandidates ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
          <AnimatePresence>
            {showCandidates && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="overflow-hidden space-y-3"
              >
                {candidates.map((c, i) => (
                  <div key={i} className="rounded-xl border border-border bg-card p-4">
                    <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                      <div>
                        <span className="font-bold">{c.symbol}</span>
                        <span className="text-muted-foreground text-sm ml-2">{c.company_name}</span>
                        {c.sector && (
                          <span className="text-xs text-muted-foreground ml-2">· {c.sector}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        {c.rsi > 0 && (
                          <span className="text-xs text-muted-foreground">
                            RSI {c.rsi.toFixed(0)}
                          </span>
                        )}
                        <RecommendationBadge recommendation={c.recommendation} />
                      </div>
                    </div>
                    <div className="text-sm font-medium mb-2">
                      {formatCurrency(c.current_price)} · Target {formatCurrency(c.target_price)}
                    </div>
                    {c.fundamentals && (
                      <p className="text-xs text-muted-foreground mb-1">
                        <span className="font-medium text-foreground/70">Fundamentals:</span>{' '}
                        {c.fundamentals}
                      </p>
                    )}
                    {c.technicals && (
                      <p className="text-xs text-muted-foreground mb-1">
                        <span className="font-medium text-foreground/70">Technicals:</span>{' '}
                        {c.technicals}
                      </p>
                    )}
                    {c.news_catalysts && (
                      <p className="text-xs text-muted-foreground mb-1">
                        <span className="font-medium text-foreground/70">News:</span>{' '}
                        {c.news_catalysts}
                      </p>
                    )}
                    {c.summary && (
                      <p className="text-xs text-muted-foreground mt-2 italic">{c.summary}</p>
                    )}
                  </div>
                ))}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* Trade proposals */}
      {!scanning && proposals.length > 0 && (
        <div className="space-y-4 mb-8">
          <AnimatePresence>
            {proposals.map((p, i) => {
              const isExecuted = executedIds.has(i);
              const canSell =
                p.action !== 'sell' || holdings.some((h) => h.symbol === p.symbol);
              const portfolioValue = computePortfolioValue(holdings);
              const sectorData = computeSectorExposure(holdings);
              const currentSectorValue =
                sectorData.sectors.find((s) => s.sector === (p.sector || 'Other'))?.value || 0;
              const sized =
                p.action === 'buy'
                  ? computeCappedPositionSize(
                      p.suggested_position_pct || 10,
                      p.current_price,
                      portfolioValue,
                      currentSectorValue
                    )
                  : { shares: 0, positionValue: 0 };
              const upsidePct =
                p.current_price > 0 && p.target_price > 0
                  ? ((p.target_price - p.current_price) / p.current_price) * 100
                  : 0;
              const downsidePct =
                p.current_price > 0 && p.stop_loss > 0
                  ? ((p.stop_loss - p.current_price) / p.current_price) * 100
                  : 0;

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
                          {p.sector && (
                            <span className="text-xs text-muted-foreground hidden sm:inline">
                              · {p.sector}
                            </span>
                          )}
                        </div>
                        <div className="text-sm text-muted-foreground mt-0.5">
                          {p.action === 'buy' ? 'Buy' : 'Sell'} {sized.shares} shares @{' '}
                          {formatCurrency(p.current_price)} ·{' '}
                          {formatCurrency(sized.positionValue)}
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

                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm mb-3">
                    <div>
                      <div className="text-xs text-muted-foreground">Confidence</div>
                      <div className="font-semibold">{p.confidence}%</div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Target</div>
                      <div className="font-semibold text-emerald-500">
                        {formatCurrency(p.target_price)}{' '}
                        <span className="text-xs">(+{upsidePct.toFixed(1)}%)</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Stop Loss</div>
                      <div className="font-semibold text-red-500">
                        {formatCurrency(p.stop_loss)}{' '}
                        <span className="text-xs">({downsidePct.toFixed(1)}%)</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Allocation</div>
                      <div className="font-semibold">
                        {p.suggested_position_pct
                          ? `${p.suggested_position_pct.toFixed(0)}%`
                          : '—'}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-muted-foreground">Total</div>
                      <div className="font-semibold">{formatCurrency(sized.positionValue)}</div>
                    </div>
                  </div>

                  {p.reasoning && (
                    <p className="text-sm text-muted-foreground leading-relaxed mb-2">
                      {p.reasoning}
                    </p>
                  )}
                  {(p.technicals || p.news_catalysts) && (
                    <div className="text-xs text-muted-foreground space-y-1 mb-3">
                      {p.technicals && (
                        <p>
                          <span className="font-medium text-foreground/70">Technicals:</span>{' '}
                          {p.technicals}
                        </p>
                      )}
                      {p.news_catalysts && (
                        <p>
                          <span className="font-medium text-foreground/70">Catalysts:</span>{' '}
                          {p.news_catalysts}
                        </p>
                      )}
                    </div>
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
          className="rounded-2xl border border-border bg-card p-12 text-center mb-8"
        >
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-accent to-primary flex items-center justify-center mx-auto mb-4 glow-accent">
            <Brain className="w-8 h-8 text-white" />
          </div>
          <h3 className="text-lg font-semibold mb-2">Autonomous mode is ready</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            The AI runs a two-pass analysis: first a deep market scan with fundamentals, technicals,
            and news; then a risk-aware portfolio fit with confidence-weighted position sizing.
          </p>
          <Button onClick={runScan} className="gap-2 bg-gradient-to-r from-primary to-accent">
            <Brain className="w-4 h-4" />
            Run First Scan
          </Button>
        </motion.div>
      )}

      {/* AI Track Record */}
      {decisions.length > 0 && <TradePerformance decisions={decisions} />}

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
                      {d.sector && (
                        <span className="text-xs text-muted-foreground ml-2">· {d.sector}</span>
                      )}
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