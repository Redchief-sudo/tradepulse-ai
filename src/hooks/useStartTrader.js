import { useState, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import {
  runPass1,
  runPass2,
  runPass3,
  runAdversarialReview,
  runCommitteeDebate,
} from '@/lib/autonomousScan';
import {
  computePortfolioValue,
  computeSectorExposure,
  computeCappedPositionSize,
} from '@/lib/portfolio';
import { profileParams } from '@/lib/tradeProfiles';

const STAGES = [
  { key: 'pass1', label: 'Pass 1: Multi-asset market scan' },
  { key: 'pass2', label: 'Pass 2: Investment committee debate' },
  { key: 'pass3', label: 'Pass 3: Portfolio fit & cross-asset' },
  { key: 'pass4', label: 'Pass 4: GNN + ML multi-factor scoring' },
  { key: 'pass5', label: 'Pass 5: Adversarial risk veto' },
  { key: 'exec', label: 'Executing approved trades' },
];

// Reusable hook that runs the full autonomous cycle:
// 5-pass AI scan → auto-execute all approved proposals.
// Used by the Dashboard "Start Trader" button and the Autonomous Trader page.
export function useStartTrader() {
  const [isRunning, setIsRunning] = useState(false);
  const [stageIndex, setStageIndex] = useState(-1);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const startTrader = useCallback(async ({ holdings, assetClasses, tradeProfile, mlWeights }) => {
    setIsRunning(true);
    setError(null);
    setResult(null);
    setStageIndex(0);

    const h = holdings || [];
    const classes = assetClasses && assetClasses.length
      ? assetClasses
      : ['stocks', 'crypto', 'forex', 'commodities', 'fixed_income'];

    try {
      // Pass 1 — Deep market scan
      const p1 = await runPass1(h, classes);
      const candidateMap = {};
      (p1.candidates || []).forEach((c) => { candidateMap[c.symbol.toUpperCase()] = c; });

      // Pass 2 — Investment committee debate
      setStageIndex(1);
      const committee = await runCommitteeDebate(p1.candidates || []);
      const debateMap = {};
      (committee.debates || []).forEach((d) => { debateMap[d.symbol.toUpperCase()] = d; });
      const consensusCandidates = (p1.candidates || []).filter(
        (c) => debateMap[c.symbol.toUpperCase()]?.consensus
      );

      // Pass 3 — Portfolio fit
      setStageIndex(2);
      const pp = profileParams(tradeProfile || 'balanced');
      const p2 = await runPass2(h, consensusCandidates, pp, p1);

      // Pass 4 — ML scoring
      setStageIndex(3);
      const p3 = await runPass3(p2.proposals || [], consensusCandidates, mlWeights);
      const scoreMap = {};
      (p3.scores || []).forEach((s) => { scoreMap[s.symbol.toUpperCase()] = s; });
      let merged = (p2.proposals || []).map((p) => ({
        ...p,
        asset_class: candidateMap[p.symbol.toUpperCase()]?.asset_class || 'stocks',
        ...(scoreMap[p.symbol.toUpperCase()] || {}),
      }));

      // Confidence threshold + daily trade cap
      const compliant = merged.filter((p) => (p.confidence || 0) >= pp.min_confidence);
      merged = compliant.slice(0, pp.max_daily_trades);

      // Pass 5 — Adversarial risk veto
      setStageIndex(4);
      const adv = await runAdversarialReview(merged, h, p1);
      const reviewMap = {};
      (adv.reviews || []).forEach((r) => { reviewMap[r.symbol.toUpperCase()] = r; });
      merged = merged.map((p) => ({
        ...p,
        adversarial_verdict: reviewMap[p.symbol.toUpperCase()]?.verdict || 'approved',
      }));
      const finalProposals = merged.filter((p) => p.adversarial_verdict !== 'vetoed');

      if (finalProposals.length === 0) {
        setStageIndex(-1);
        setResult({ proposals: [], executed: 0, marketSummary: p1.market_summary });
        return { proposals: [], executed: 0 };
      }

      // Execute all approved proposals
      setStageIndex(5);
      let executed = 0;
      for (const proposal of finalProposals) {
        const portfolioValue = computePortfolioValue(h);
        const sectorData = computeSectorExposure(h);
        const currentSectorValue =
          sectorData.sectors.find((s) => s.sector === (proposal.sector || 'Other'))?.value || 0;

        let qty;
        if (proposal.action === 'buy') {
          const sized = computeCappedPositionSize(
            proposal.suggested_position_pct || 10,
            proposal.current_price,
            portfolioValue,
            currentSectorValue,
            pp.max_sector_pct / 100,
            pp.max_position_pct / 100
          );
          qty = sized.shares;
        } else {
          const existing = h.find((x) => x.symbol === proposal.symbol);
          qty = existing ? Math.min(proposal.shares || existing.shares, existing.shares) : 0;
        }
        if (qty <= 0) continue;

        try {
          const res = await base44.functions.invoke('executeTrade', {
            symbol: proposal.symbol,
            action: proposal.action,
            qty,
            price: proposal.current_price,
            company_name: proposal.company_name,
            sector: proposal.sector,
            confidence: proposal.confidence,
            target_price: proposal.target_price,
            stop_loss: proposal.stop_loss,
            ai_recommended: true,
            source: 'start_trader',
            reasoning: proposal.reasoning,
            ml_score: proposal.ml_score,
            technical_score: proposal.technical_score,
            momentum_score: proposal.momentum_score,
            risk_score: proposal.risk_score,
            recordDecision: true,
          });
          if (res?.data?.status === 'filled' || res?.data?.status === 'paper_filled') executed++;
        } catch (e) {
          console.error('Trade execution failed:', e);
        }
      }

      setStageIndex(-1);
      setResult({ proposals: finalProposals, executed, marketSummary: p1.market_summary });
      return { proposals: finalProposals, executed };
    } catch (e) {
      console.error(e);
      setError(e?.message || String(e) || 'Scan failed');
      setStageIndex(-1);
      return { proposals: [], executed: 0, error: e?.message || String(e) };
    } finally {
      setIsRunning(false);
    }
  }, []);

  return { isRunning, stageIndex, stageLabel: stageIndex >= 0 ? STAGES[stageIndex]?.label : null, error, result, startTrader };
}