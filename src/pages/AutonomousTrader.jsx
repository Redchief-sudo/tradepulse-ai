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
  ChevronDown,
  ChevronUp,
  Shield,
  Activity,
  Cpu,
  Layers,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import TradePerformance from '@/components/TradePerformance';
import TradeProposalCard from '@/components/TradeProposalCard';
import CandidateCard from '@/components/CandidateCard';
import StopLossScanner from '@/components/StopLossScanner';
import { runPass1, runPass2, runPass3, runAdversarialReview, runCommitteeDebate } from '@/lib/autonomousScan';
import {
  computePortfolioValue,
  computeSectorExposure,
  computeCappedPositionSize,
  formatCurrency,
} from '@/lib/portfolio';
import { cn } from '@/lib/utils';
import { getProfile, profileParams } from '@/lib/tradeProfiles';
import ActiveProfileBanner from '@/components/ActiveProfileBanner';
import ArchitectureOverview from '@/components/ArchitectureOverview';
import RegimeBanner from '@/components/RegimeBanner';
import StressTestSimulator from '@/components/StressTestSimulator';
import AssetClassSelector from '@/components/AssetClassSelector';
import SelfLearningMemory from '@/components/SelfLearningMemory';

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

const SCAN_STAGES = [
  { key: 'pass1', label: 'Pass 1: Multi-asset market scan', model: 'Gemini 3.1 Pro · Transformer' },
  { key: 'pass2', label: 'Pass 2: Investment committee debate', model: 'Claude Sonnet 5 · 4 Archetypes' },
  { key: 'pass3', label: 'Pass 3: RL execution & cross-asset fit', model: 'Claude Sonnet 5 · RL-DQN' },
  { key: 'pass4', label: 'Pass 4: GNN + ML multi-factor scoring', model: 'Claude Sonnet 5 · GNN ensemble' },
  { key: 'pass5', label: 'Pass 5: Adversarial risk veto', model: 'Claude Sonnet 5 · Risk Officer' },
];

export default function AutonomousTrader() {
  const [scanning, setScanning] = useState(false);
  const [scanStageIndex, setScanStageIndex] = useState(-1);
  const [executing, setExecuting] = useState(false);
  const [marketSummary, setMarketSummary] = useState('');
  const [riskAssessment, setRiskAssessment] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [proposals, setProposals] = useState([]);
  const [holdings, setHoldings] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [executedIds, setExecutedIds] = useState(new Set());
  const [showCandidates, setShowCandidates] = useState(false);
  const [stopLossPct, setStopLossPct] = useState(8);
  const [tradeProfile, setTradeProfile] = useState('balanced');
  const [filteredCount, setFilteredCount] = useState(0);
  const [showArchitecture, setShowArchitecture] = useState(false);
  const [marketRegime, setMarketRegime] = useState(null);
  const [vetoedCount, setVetoedCount] = useState(0);
  const [assetClasses, setAssetClasses] = useState([]);
  const [mlWeights, setMlWeights] = useState(null);
  const [broker, setBroker] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const [h, d, me] = await Promise.all([
        base44.entities.Holding.list(),
        base44.entities.AITradeDecision.list('-created_date', 30),
        base44.auth.me(),
      ]);
      setHoldings(h || []);
      setDecisions(d || []);
      if (me.stop_loss_pct) setStopLossPct(me.stop_loss_pct);
      if (me.trade_profile) setTradeProfile(me.trade_profile);
      if (me.broker === 'alpaca' && me.broker_api_key) setBroker({ mode: me.broker_mode || 'paper' });
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const saveStopLossPct = async (pct) => {
    setStopLossPct(pct);
    try {
      await base44.auth.updateMe({ stop_loss_pct: pct });
    } catch (e) {
      console.error(e);
    }
  };

  const runScan = async () => {
    setScanning(true);
    setProposals([]);
    setCandidates([]);
    setMarketSummary('');
    setRiskAssessment('');
    setExecutedIds(new Set());
    setMarketRegime(null);
    setVetoedCount(0);

    try {
      setScanStageIndex(0);
      const p1 = await runPass1(holdings, assetClasses);
      setMarketSummary(p1.market_summary || '');
      setCandidates(p1.candidates || []);
      setMarketRegime({
        market_regime: p1.market_regime,
        regime_confidence: p1.regime_confidence,
        regime_strategy: p1.regime_strategy,
      });
      const candidateMap = {};
      (p1.candidates || []).forEach((c) => { candidateMap[c.symbol.toUpperCase()] = c; });

      // Pass 2: Investment Committee Debate (4-archetype consensus)
      setScanStageIndex(1);
      const committee = await runCommitteeDebate(p1.candidates || []);
      const debateMap = {};
      (committee.debates || []).forEach((d) => { debateMap[d.symbol.toUpperCase()] = d; });
      const consensusCandidates = (p1.candidates || []).filter(
        (c) => debateMap[c.symbol.toUpperCase()]?.consensus
      );

      // Pass 3: Portfolio fit + cross-asset correlation
      setScanStageIndex(2);
      const pp = profileParams(tradeProfile);
      const p2 = await runPass2(holdings, consensusCandidates, pp, p1);
      setRiskAssessment(p2.risk_assessment || '');

      // Pass 4: ML scoring with self-learning weights
      setScanStageIndex(3);
      const p3 = await runPass3(p2.proposals || [], consensusCandidates, mlWeights);
      const scoreMap = {};
      (p3.scores || []).forEach((s) => { scoreMap[s.symbol.toUpperCase()] = s; });
      let merged = (p2.proposals || []).map((p) => ({
        ...p,
        asset_class: candidateMap[p.symbol.toUpperCase()]?.asset_class || 'stocks',
        microstructure_signal: candidateMap[p.symbol.toUpperCase()]?.microstructure_signal,
        committee_debate: debateMap[p.symbol.toUpperCase()],
        ...(scoreMap[p.symbol.toUpperCase()] || {}),
      }));

      // Apply institutional confidence threshold + daily trade cap
      const compliant = merged.filter((p) => (p.confidence || 0) >= pp.min_confidence);
      setFilteredCount(merged.length - compliant.length);
      merged = compliant.slice(0, pp.max_daily_trades);

      // Pass 5: Adversarial Risk Officer veto layer
      setScanStageIndex(4);
      const adv = await runAdversarialReview(merged, holdings, p1);
      const reviewMap = {};
      (adv.reviews || []).forEach((r) => { reviewMap[r.symbol.toUpperCase()] = r; });
      merged = merged.map((p) => ({
        ...p,
        adversarial_verdict: reviewMap[p.symbol.toUpperCase()]?.verdict || 'approved',
        adversarial_note: reviewMap[p.symbol.toUpperCase()]?.note || '',
      }));
      const vetoed = merged.filter((p) => p.adversarial_verdict === 'vetoed');
      setVetoedCount(vetoed.length);
      setProposals(merged.filter((p) => p.adversarial_verdict !== 'vetoed'));
    } catch (e) {
      console.error(e);
    }
    setScanStageIndex(-1);
    setScanning(false);
  };

  const executeProposal = async (proposal, index) => {
    const portfolioValue = computePortfolioValue(holdings);
    const sectorData = computeSectorExposure(holdings);
    const currentSectorValue =
      sectorData.sectors.find((s) => s.sector === (proposal.sector || 'Other'))?.value || 0;

    let shares, positionValue;
    if (proposal.action === 'buy') {
      const pp = profileParams(tradeProfile);
      const sized = computeCappedPositionSize(
        proposal.suggested_position_pct || 10,
        proposal.current_price,
        portfolioValue,
        currentSectorValue,
        pp.max_sector_pct / 100,
        pp.max_position_pct / 100
      );
      shares = sized.shares;
      positionValue = sized.positionValue;
    } else {
      const existing = holdings.find((h) => h.symbol === proposal.symbol);
      shares = existing ? Math.min(proposal.shares || existing.shares, existing.shares) : 0;
      positionValue = shares * proposal.current_price;
    }

    if (shares <= 0) return;

    // Route through the real broker if Alpaca is connected (paper or live).
    if (broker) {
      try {
        await base44.functions.invoke('executeBrokerOrder', {
          symbol: proposal.symbol,
          qty: shares,
          side: proposal.action,
        });
      } catch (e) {
        console.error('Broker order failed, recording as paper:', e);
      }
    }

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
      asset_class: proposal.asset_class || 'stocks',
      action: proposal.action,
      shares,
      price: proposal.current_price,
      position_value: positionValue,
      confidence: proposal.confidence,
      target_price: proposal.target_price,
      stop_loss: proposal.stop_loss,
      reasoning: proposal.reasoning,
      status: 'executed',
      ml_score: proposal.ml_score,
      technical_score: proposal.technical_score,
      fundamental_score: proposal.fundamental_score,
      sentiment_score: proposal.sentiment_score,
      momentum_score: proposal.momentum_score,
      risk_score: proposal.risk_score,
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
            <span className="text-xs font-normal px-2 py-1 rounded-full bg-accent/15 text-accent border border-accent/30 flex items-center gap-1">
              <Cpu className="w-3 h-3" /> ML Engine
            </span>
          </h1>
          <p className="text-muted-foreground text-sm mt-1">
            3-pass AI: Gemini 3.1 Pro scan → Claude Sonnet 5 fit → ML multi-factor scoring
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button
            onClick={() => setShowArchitecture(!showArchitecture)}
            variant="outline"
            size="sm"
            className="gap-1.5"
          >
            <Layers className="w-4 h-4" />
            {showArchitecture ? 'Hide' : 'Architecture'}
          </Button>
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

      <AssetClassSelector value={assetClasses} onChange={setAssetClasses} />

      <StopLossScanner
        holdings={holdings}
        stopLossPct={stopLossPct}
        onStopLossPctChange={saveStopLossPct}
        onHoldingsChange={loadData}
      />

      {/* Active risk profile */}
      <ActiveProfileBanner profile={getProfile(tradeProfile)} filteredCount={filteredCount} />

      {/* Market regime detection */}
      {marketRegime && <RegimeBanner regime={marketRegime} vetoedCount={vetoedCount} />}

      {/* Institutional architecture overview */}
      {showArchitecture && <ArchitectureOverview onClose={() => setShowArchitecture(false)} />}

      {/* Generative stress-test simulation */}
      {holdings.length > 0 && !scanning && <StressTestSimulator holdings={holdings} />}

      {/* Self-learning model memory */}
      <SelfLearningMemory decisions={decisions} onWeights={setMlWeights} />

      {/* Scanning state with pass indicators */}
      {scanning && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-accent/30 bg-gradient-to-br from-accent/10 to-primary/5 p-6 md:p-8 mb-6"
        >
          <div className="flex items-center gap-3 mb-4">
            <motion.div
              animate={{ scale: [1, 1.1, 1], opacity: [0.7, 1, 0.7] }}
              transition={{ duration: 2, repeat: Infinity }}
              className="w-12 h-12 rounded-xl bg-gradient-to-br from-accent to-primary flex items-center justify-center glow-accent"
            >
              <Brain className="w-6 h-6 text-white" />
            </motion.div>
            <div>
              <h3 className="font-semibold">
                {scanStageIndex >= 0 ? SCAN_STAGES[scanStageIndex].label : 'Initializing...'}
              </h3>
              <p className="text-xs text-muted-foreground">
                {scanStageIndex >= 0 && `Model: ${SCAN_STAGES[scanStageIndex].model}`}
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            {SCAN_STAGES.map((s, i) => (
              <div key={s.key} className="flex-1">
                <div
                  className={cn(
                    'h-1.5 rounded-full transition-all',
                    i < scanStageIndex
                      ? 'bg-emerald-500'
                      : i === scanStageIndex
                      ? 'bg-accent animate-pulse'
                      : 'bg-muted'
                  )}
                />
                <p
                  className={cn(
                    'text-xs mt-1.5 transition-colors hidden md:block',
                    i <= scanStageIndex ? 'text-foreground' : 'text-muted-foreground'
                  )}
                >
                  {s.label.replace(/Pass \d: /, '')}
                </p>
              </div>
            ))}
          </div>
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
                  <CandidateCard key={i} candidate={c} index={i} />
                ))}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* Trade proposals with ML scores */}
      {!scanning && proposals.length > 0 && (
        <div className="space-y-4 mb-8">
          <AnimatePresence>
            {proposals.map((p, i) => (
              <TradeProposalCard
                key={i}
                proposal={p}
                index={i}
                isExecuted={executedIds.has(i)}
                isExecuting={executing}
                holdings={holdings}
                onExecute={executeProposal}
              />
            ))}
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
          <h3 className="text-lg font-semibold mb-2">ML-Powered Autonomous Trading</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            Three AI passes work together: Gemini 3.1 Pro scans the market with real-time data and
            full technicals, Claude Sonnet 5 fits trades to your portfolio with risk-aware sizing,
            then a multi-factor ML engine scores each trade across technical, fundamental,
            sentiment, momentum, and risk factors.
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
                      {d.ml_score !== undefined && (
                        <span className="ml-2 text-accent">· ML {d.ml_score.toFixed(0)}</span>
                      )}
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