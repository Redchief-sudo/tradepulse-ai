import React from 'react';
import { motion } from 'framer-motion';
import { Zap, CheckCircle2, ArrowDownRight, ArrowUpRight, ShieldAlert, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import RecommendationBadge from '@/components/RecommendationBadge';
import MLScoreCard from '@/components/MLScoreCard';
import { computePortfolioValue, computeSectorExposure, computeCappedPositionSize, formatCurrency } from '@/lib/portfolio';
import { cn } from '@/lib/utils';

export default function TradeProposalCard({ proposal, index, isExecuted, isExecuting, holdings, onExecute }) {
  const portfolioValue = computePortfolioValue(holdings);
  const sectorData = computeSectorExposure(holdings);
  const currentSectorValue =
    sectorData.sectors.find((s) => s.sector === (proposal.sector || 'Other'))?.value || 0;
  const sized =
    proposal.action === 'buy'
      ? computeCappedPositionSize(
          proposal.suggested_position_pct || 10,
          proposal.current_price,
          portfolioValue,
          currentSectorValue
        )
      : { shares: 0, positionValue: 0 };

  const canSell = proposal.action !== 'sell' || holdings.some((h) => h.symbol === proposal.symbol);
  const upsidePct =
    proposal.current_price > 0 && proposal.target_price > 0
      ? ((proposal.target_price - proposal.current_price) / proposal.current_price) * 100
      : 0;
  const downsidePct =
    proposal.current_price > 0 && proposal.stop_loss > 0
      ? ((proposal.stop_loss - proposal.current_price) / proposal.current_price) * 100
      : 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.08 }}
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
              proposal.action === 'buy'
                ? 'bg-emerald-500/10 text-emerald-500'
                : 'bg-red-500/10 text-red-500'
            )}
          >
            {proposal.action === 'buy' ? (
              <ArrowDownRight className="w-5 h-5" />
            ) : (
              <ArrowUpRight className="w-5 h-5" />
            )}
          </div>
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-bold text-base">{proposal.symbol}</span>
              <span className="text-xs text-muted-foreground">{proposal.company_name}</span>
              {proposal.sector && (
                <span className="text-xs text-muted-foreground hidden sm:inline">
                  · {proposal.sector}
                </span>
              )}
            </div>
            <div className="text-sm text-muted-foreground mt-0.5">
              {proposal.action === 'buy' ? 'Buy' : 'Sell'} {sized.shares} shares @{' '}
              {formatCurrency(proposal.current_price)} · {formatCurrency(sized.positionValue)}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <RecommendationBadge recommendation={proposal.recommendation} />
          {proposal.adversarial_verdict === 'flagged' && (
            <span className="text-xs px-2 py-1 rounded-full border bg-amber-500/15 text-amber-400 border-amber-500/30 flex items-center gap-1">
              <ShieldAlert className="w-3 h-3" /> Risk Flag
            </span>
          )}
          {proposal.adversarial_verdict === 'approved' && proposal.adversarial_note && (
            <span className="text-xs px-2 py-1 rounded-full border bg-emerald-500/15 text-emerald-400 border-emerald-500/30 flex items-center gap-1">
              <ShieldCheck className="w-3 h-3" /> Cleared
            </span>
          )}
          {isExecuted && (
            <span className="flex items-center gap-1 text-xs text-emerald-500 font-medium">
              <CheckCircle2 className="w-4 h-4" /> Executed
            </span>
          )}
        </div>
      </div>

      <MLScoreCard scores={proposal} />

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm mb-3">
        <div>
          <div className="text-xs text-muted-foreground">Confidence</div>
          <div className="font-semibold">{proposal.confidence}%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Target</div>
          <div className="font-semibold text-emerald-500">
            {formatCurrency(proposal.target_price)}{' '}
            <span className="text-xs">(+{upsidePct.toFixed(1)}%)</span>
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Stop Loss</div>
          <div className="font-semibold text-red-500">
            {formatCurrency(proposal.stop_loss)}{' '}
            <span className="text-xs">({downsidePct.toFixed(1)}%)</span>
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Allocation</div>
          <div className="font-semibold">
            {proposal.suggested_position_pct
              ? `${proposal.suggested_position_pct.toFixed(0)}%`
              : '—'}
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Total</div>
          <div className="font-semibold">{formatCurrency(sized.positionValue)}</div>
        </div>
      </div>

      {proposal.reasoning && (
        <p className="text-sm text-muted-foreground leading-relaxed mb-2">{proposal.reasoning}</p>
      )}
      {proposal.adversarial_note && (
        <div
          className={cn(
            'text-xs rounded-lg p-2 mb-2 border flex items-start gap-1.5',
            proposal.adversarial_verdict === 'flagged'
              ? 'bg-amber-500/10 border-amber-500/20 text-amber-400'
              : 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
          )}
        >
          <ShieldAlert className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
          <span>
            <span className="font-medium">Adversarial Risk Officer:</span> {proposal.adversarial_note}
          </span>
        </div>
      )}
      {(proposal.technicals || proposal.news_catalysts) && (
        <div className="text-xs text-muted-foreground space-y-1 mb-3">
          {proposal.technicals && (
            <p>
              <span className="font-medium text-foreground/70">Technicals:</span>{' '}
              {proposal.technicals}
            </p>
          )}
          {proposal.news_catalysts && (
            <p>
              <span className="font-medium text-foreground/70">Catalysts:</span>{' '}
              {proposal.news_catalysts}
            </p>
          )}
        </div>
      )}

      {!isExecuted && (
        <Button
          size="sm"
          onClick={() => onExecute(proposal, index)}
          disabled={isExecuting || !canSell}
          variant="secondary"
          className="gap-1.5"
        >
          <Zap className="w-3.5 h-3.5" />
          {canSell ? 'Execute Trade' : 'Cannot sell — not held'}
        </Button>
      )}
    </motion.div>
  );
}