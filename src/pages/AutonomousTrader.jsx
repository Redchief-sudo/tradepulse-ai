import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import {
  TrendingUp,
  TrendingDown,
  History,
  Cpu,
  Layers,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import TradePerformance from '@/components/TradePerformance';
import StopLossScanner from '@/components/StopLossScanner';
import TradingSessionControl from '@/components/TradingSessionControl';
import ScanRunStatus from '@/components/ScanRunStatus';
import { formatCurrency } from '@/lib/portfolio';
import { cn } from '@/lib/utils';
import { getProfile } from '@/lib/tradeProfiles';
import ActiveProfileBanner from '@/components/ActiveProfileBanner';
import ArchitectureOverview from '@/components/ArchitectureOverview';
import RegimeBanner from '@/components/RegimeBanner';
import StressTestSimulator from '@/components/StressTestSimulator';
import BacktestPanel from '@/components/BacktestPanel';
import SelfLearningMemory from '@/components/SelfLearningMemory';
import OutcomeAnalytics from '@/components/OutcomeAnalytics';

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

// NOTE: this page no longer runs its own client-side scan/execute pipeline.
// All autonomous scanning and execution is coordinator-driven — this page
// only queues a scan request (via TradingSessionControl / useStartTrader)
// and displays the coordinator's persisted results (ScanRunStatus, Decision
// History, AI Track Record). Fixes a prior defect: this page used to run a
// separate, client-side 5-pass LLM pipeline and call executeTrade directly,
// bypassing the ScanRequest queue, the coordinator's lock, and the autonomy
// gate (user.trading_active) entirely.
export default function AutonomousTrader() {
  const [holdings, setHoldings] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [stopLossPct, setStopLossPct] = useState(8);
  const [tradeProfile, setTradeProfile] = useState('balanced');
  const [showArchitecture, setShowArchitecture] = useState(false);
  const [marketRegime, setMarketRegime] = useState(null);
  const [mlWeights, setMlWeights] = useState(null);

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
    } catch (e) {
      console.error(e);
    }
  }, []);

  const loadRegime = useCallback(async () => {
    try {
      const response = await base44.functions.invoke('getMarketRegime', {});
      const data = response?.data || response;
      if (data?.ok) setMarketRegime(data.regime);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    loadData();
    loadRegime();
  }, [loadData, loadRegime]);

  const saveStopLossPct = async (pct) => {
    setStopLossPct(pct);
    try {
      await base44.auth.updateMe({ stop_loss_pct: pct });
    } catch (e) {
      console.error(e);
    }
  };

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
            Coordinator-driven: scans run on a schedule (or on demand via Start Trading below) and
            execute automatically once approved — this page monitors the results.
          </p>
        </div>
        <Button
          onClick={() => setShowArchitecture(!showArchitecture)}
          variant="outline"
          size="sm"
          className="gap-1.5"
        >
          <Layers className="w-4 h-4" />
          {showArchitecture ? 'Hide' : 'Architecture'}
        </Button>
      </div>

      {/* Single coordinated start/stop control — same component and same
          ScanRequest-queuing flow as the Dashboard. There is no longer a
          separate, page-local trading trigger. */}
      <TradingSessionControl onStart={loadData} onComplete={loadData} />

      <StopLossScanner
        holdings={holdings}
        stopLossPct={stopLossPct}
        onStopLossPctChange={saveStopLossPct}
        onHoldingsChange={loadData}
      />

      {/* Active risk profile */}
      <ActiveProfileBanner profile={getProfile(tradeProfile)} />

      {/* Market regime detection — real deterministic classification, not
          pipeline output */}
      {marketRegime && <RegimeBanner regime={marketRegime} />}

      {/* Institutional architecture overview */}
      {showArchitecture && <ArchitectureOverview onClose={() => setShowArchitecture(false)} />}

      {/* Generative stress-test simulation */}
      {holdings.length > 0 && <StressTestSimulator holdings={holdings} />}

      {/* Self-learning model memory */}
      <SelfLearningMemory decisions={decisions} onWeights={setMlWeights} />

      {/* Deterministic strategy backtest + walk-forward */}
      <BacktestPanel />

      {/* Coordinator scan status — authoritative persisted ScanRun/ScanRequest
          state, not client-held pipeline state */}
      <div className="mb-6">
        <ScanRunStatus />
      </div>

      {/* AI Track Record */}
      {decisions.length > 0 && <TradePerformance decisions={decisions} />}

      {/* Outcome labeling analytics (alpha vs benchmark, factor validation) */}
      <OutcomeAnalytics />

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
