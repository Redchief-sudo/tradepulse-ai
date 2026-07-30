import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Users, ChevronDown, ChevronUp, Check, X, Minus } from 'lucide-react';
import { cn } from '@/lib/utils';

const PERSONA_STYLE = {
  'Value/Utility Specialist': { color: 'text-emerald-400', tag: 'Buffett' },
  'Macro Contrarian': { color: 'text-amber-400', tag: 'Burry' },
  'Quant Statistician': { color: 'text-blue-400', tag: 'Simmons' },
  'Tail-Risk Hedger': { color: 'text-red-400', tag: 'Taleb' },
};

function VoteIcon({ vote }) {
  if (vote === 'bullish') return <Check className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0 mt-0.5" />;
  if (vote === 'bearish') return <X className="w-3.5 h-3.5 text-red-500 flex-shrink-0 mt-0.5" />;
  return <Minus className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0 mt-0.5" />;
}

export default function CommitteeDebate({ debate }) {
  const [open, setOpen] = useState(false);
  if (!debate) return null;
  const consensus = debate.consensus;
  const votes = debate.consensus_votes || 0;
  return (
    <div className="rounded-xl border border-border bg-muted/20 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-2.5 hover:bg-muted/30 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Users className="w-3.5 h-3.5 text-accent" />
          <span className="text-xs font-semibold">Investment Committee Debate</span>
          <span
            className={cn(
              'text-xs px-1.5 py-0.5 rounded-full border',
              consensus
                ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                : 'bg-red-500/10 text-red-400 border-red-500/30'
            )}
          >
            {votes}/4 {consensus ? 'Consensus' : 'No Consensus'}
          </span>
        </div>
        {open ? (
          <ChevronUp className="w-4 h-4 text-muted-foreground" />
        ) : (
          <ChevronDown className="w-4 h-4 text-muted-foreground" />
        )}
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="p-2.5 pt-1 space-y-2">
              {(debate.personas || []).map((p) => {
                const style = PERSONA_STYLE[p.name] || {};
                return (
                  <div key={p.name} className="flex items-start gap-2 text-xs">
                    <VoteIcon vote={p.vote} />
                    <div className="flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className={cn('font-medium', style.color)}>{p.name}</span>
                        <span className="text-muted-foreground">
                          · {style.tag} · {p.vote} · {p.conviction}%
                        </span>
                      </div>
                      <p className="text-muted-foreground mt-0.5 leading-snug">{p.argument}</p>
                    </div>
                  </div>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}