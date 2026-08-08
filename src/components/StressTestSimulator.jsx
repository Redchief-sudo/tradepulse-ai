import React, { useState } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import { FlaskConical, Loader2, TrendingDown, AlertTriangle, Play } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { computePortfolioValue } from '@/lib/portfolio';
import { cn } from '@/lib/utils';

const SCENARIOS = [
  '2008-style financial crisis',
  'sudden geopolitical conflict / war',
  'major bank failure (systemic)',
  'flash crash (intraday -10%)',
  'aggressive rate-hike shock',
];

export default function StressTestSimulator({ holdings }) {
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState([]);

  const runSimulation = async () => {
    setRunning(true);
    setResults([]);
    try {
      const portfolioValue = computePortfolioValue(holdings);
      const positions = holdings.map((h) => ({
        symbol: h.symbol,
        sector: h.sector,
        shares: h.shares,
        avg_price: h.avg_price,
        current_price: h.current_price || h.avg_price,
      }));
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Generate qualitative stress scenarios for the following portfolio. These are LLM estimates, not outputs from a trained GAN or calibrated risk model.

Portfolio value: $${portfolioValue.toFixed(0)}
Positions: ${JSON.stringify(positions, null, 2)}

For each of these scenarios, project the portfolio impact and identify the most vulnerable holdings:
${SCENARIOS.map((s, i) => `${i + 1}. ${s}`).join('\n')}

For each scenario return: name, a brief description of the synthetic shock, the projected portfolio impact (negative %), the 2-3 most vulnerable symbols, and a 1-line pre-computed playbook action the AI should take.`,
        model: 'claude_sonnet_4_6',
        response_json_schema: {
          type: 'object',
          properties: {
            scenarios: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  name: { type: 'string' },
                  description: { type: 'string' },
                  portfolio_impact_pct: { type: 'number' },
                  most_vulnerable: { type: 'array', items: { type: 'string' } },
                  playbook: { type: 'string' },
                },
              },
            },
          },
        },
      });
      setResults(result.scenarios || []);
    } catch (e) {
      console.error(e);
    }
    setRunning(false);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-border bg-card p-5 mb-6"
    >
      <div className="flex items-center justify-between flex-wrap gap-3 mb-2">
        <div className="flex items-center gap-2">
          <FlaskConical className="w-4 h-4 text-accent" />
          <h3 className="font-semibold text-sm">LLM Scenario Exploration</h3>
        </div>
        <Button
          size="sm"
          variant="secondary"
          onClick={runSimulation}
          disabled={running}
          className="gap-1.5"
        >
          {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
          {running ? 'Simulating...' : 'Run Synthetic Scenarios'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
        Qualitative, uncalibrated LLM estimates for exploring black-swan narratives. Do not treat
        projected losses as measured risk forecasts.
      </p>

      <AnimatePresence>
        {results.length > 0 && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="space-y-2"
          >
            {results.map((s, i) => (
              <div key={i} className="rounded-xl border border-border bg-muted/20 p-3">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="flex items-center gap-2">
                    <TrendingDown className="w-4 h-4 text-red-500" />
                    <span className="font-medium text-sm">{s.name}</span>
                  </div>
                  <span
                    className={cn(
                      'text-sm font-bold',
                      (s.portfolio_impact_pct || 0) < -15 ? 'text-red-500' : 'text-amber-400'
                    )}
                  >
                    {(s.portfolio_impact_pct || 0).toFixed(1)}%
                  </span>
                </div>
                {s.description && (
                  <p className="text-xs text-muted-foreground mt-1">{s.description}</p>
                )}
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {(s.most_vulnerable || []).map((sym) => (
                    <span
                      key={sym}
                      className="text-xs px-2 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20"
                    >
                      {sym}
                    </span>
                  ))}
                </div>
                {s.playbook && (
                  <p className="text-xs text-emerald-400 mt-2 flex items-start gap-1.5">
                    <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                    <span>
                      <span className="font-medium">Playbook:</span> {s.playbook}
                    </span>
                  </p>
                )}
              </div>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
