import React, { useState, useRef, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion, AnimatePresence } from 'framer-motion';
import { Bot, Send, Sparkles, Loader2, TrendingUp, User, Zap } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import RecommendationBadge from '@/components/RecommendationBadge';
import { cn } from '@/lib/utils';

const SYSTEM_PROMPT = `You are AlphaTrade AI, an elite AI-powered stock market trading assistant with access to real-time market data via web search.

Your role:
- Analyze stocks using real-time prices, fundamentals, technicals, and market sentiment
- Provide specific BUY/SELL/HOLD recommendations with confidence scores (0-100)
- Suggest entry prices, target prices, and stop-loss levels
- Identify key risks and upcoming catalysts
- Answer questions about market trends, sectors, and individual stocks

Always be specific with numbers. Always mention both upside and risks. Be professional but accessible.

For any stock you analyze, include it in the "analysis" array with current price, recommendation, confidence, and target price.`;

const SUGGESTIONS = [
  'Should I buy NVIDIA right now?',
  'Analyze my portfolio risk',
  'What are the best AI stocks for 2026?',
  'Compare Tesla vs Rivian',
];

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
  }).format(n || 0);
}

export default function AIAssistant() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [holdings, setHoldings] = useState([]);
  const scrollRef = useRef(null);

  useEffect(() => {
    base44.entities.Holding.list()
      .then((h) => setHoldings(h || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const sendMessage = useCallback(
    async (text) => {
      if (!text.trim() || loading) return;

      const userMsg = { role: 'user', content: text };
      setMessages((prev) => [...prev, userMsg]);
      setInput('');
      setLoading(true);

      try {
        const portfolioContext =
          holdings.length > 0
            ? `\n\nUser's current portfolio:\n${holdings
                .map(
                  (h) =>
                    `${h.symbol} (${h.company_name}): ${h.shares} shares @ $${h.avg_price} avg, current $${h.current_price || h.avg_price}`
                )
                .join('\n')}`
            : '\n\nUser has no current positions.';

        const result = await base44.integrations.Core.InvokeLLM({
          prompt: SYSTEM_PROMPT + portfolioContext + `\n\nUser question: ${text}`,
          add_context_from_internet: true,
          model: 'gemini_3_flash',
          response_json_schema: {
            type: 'object',
            properties: {
              message: {
                type: 'string',
                description: 'Your detailed response with analysis and reasoning',
              },
              analysis: {
                type: 'array',
                description: 'Stock analyses for any stocks mentioned',
                items: {
                  type: 'object',
                  properties: {
                    symbol: { type: 'string' },
                    company_name: { type: 'string' },
                    current_price: { type: 'number' },
                    recommendation: {
                      type: 'string',
                      enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
                    },
                    confidence: { type: 'number', description: '0-100 confidence score' },
                    target_price: { type: 'number' },
                    summary: { type: 'string', description: 'Brief analysis summary' },
                  },
                },
              },
            },
            required: ['message'],
          },
        });

        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: result.message,
            analysis: result.analysis || [],
          },
        ]);
      } catch (e) {
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: 'I encountered an error analyzing that. Please try again.',
          },
        ]);
      }
      setLoading(false);
    },
    [holdings, loading]
  );

  const executeTrade = async (analysis) => {
    try {
      const shares = 10;
      const trade = {
        symbol: analysis.symbol,
        company_name: analysis.company_name || analysis.symbol,
        action: 'buy',
        shares,
        price: analysis.current_price,
        total_value: shares * analysis.current_price,
        ai_recommended: true,
      };
      await base44.entities.Trade.create(trade);

      const existing = holdings.find((h) => h.symbol === analysis.symbol);
      if (existing) {
        const totalShares = existing.shares + shares;
        const totalCost = existing.shares * existing.avg_price + shares * analysis.current_price;
        const newAvg = totalCost / totalShares;
        await base44.entities.Holding.update(existing.id, {
          shares: totalShares,
          avg_price: newAvg,
          current_price: analysis.current_price,
        });
      } else {
        await base44.entities.Holding.create({
          symbol: analysis.symbol,
          company_name: analysis.company_name || analysis.symbol,
          shares,
          avg_price: analysis.current_price,
          current_price: analysis.current_price,
          sector: '',
          day_change_percent: 0,
        });
      }
      const refreshed = await base44.entities.Holding.list();
      setHoldings(refreshed || []);
    } catch (e) {
      console.error(e);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] md:h-screen">
      <div className="border-b border-border bg-card/50 backdrop-blur-sm p-4 md:p-6">
        <div className="max-w-4xl mx-auto flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent to-primary flex items-center justify-center glow-accent">
            <Bot className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="font-bold font-heading text-lg">AI Trader</h1>
            <p className="text-xs text-muted-foreground">Real-time market intelligence</p>
          </div>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto scrollbar-thin p-4 md:p-6">
        <div className="max-w-4xl mx-auto space-y-6">
          {messages.length === 0 && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="text-center py-12"
            >
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-accent to-primary flex items-center justify-center mx-auto mb-4 glow-accent">
                <Sparkles className="w-8 h-8 text-white" />
              </div>
              <h2 className="text-xl font-bold mb-2">Ask the AI Trader</h2>
              <p className="text-muted-foreground text-sm max-w-md mx-auto mb-8">
                Get real-time stock analysis, trading recommendations, and portfolio insights powered by
                live market data.
              </p>
              <div className="grid sm:grid-cols-2 gap-3 max-w-2xl mx-auto">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => sendMessage(s)}
                    className="text-left p-4 rounded-xl border border-border bg-card hover:border-primary/40 hover:bg-muted/30 transition-all text-sm"
                  >
                    <Zap className="w-4 h-4 text-accent mb-2" />
                    {s}
                  </button>
                ))}
              </div>
            </motion.div>
          )}

          <AnimatePresence>
            {messages.map((msg, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                className={cn('flex gap-3', msg.role === 'user' && 'flex-row-reverse')}
              >
                <div
                  className={cn(
                    'w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0',
                    msg.role === 'user'
                      ? 'bg-secondary'
                      : 'bg-gradient-to-br from-accent to-primary'
                  )}
                >
                  {msg.role === 'user' ? (
                    <User className="w-4 h-4" />
                  ) : (
                    <Bot className="w-4 h-4 text-white" />
                  )}
                </div>
                <div
                  className={cn(
                    'max-w-[85%] space-y-3',
                    msg.role === 'user' && 'flex flex-col items-end'
                  )}
                >
                  <div
                    className={cn(
                      'rounded-2xl p-4 text-sm leading-relaxed whitespace-pre-wrap',
                      msg.role === 'user'
                        ? 'bg-primary text-primary-foreground rounded-tr-sm'
                        : 'bg-card border border-border rounded-tl-sm'
                    )}
                  >
                    {msg.content}
                  </div>
                  {msg.analysis && msg.analysis.length > 0 && (
                    <div className="space-y-3">
                      {msg.analysis.map((a, j) => (
                        <div
                          key={j}
                          className="rounded-xl border border-border bg-card p-4 space-y-3"
                        >
                          <div className="flex items-center justify-between flex-wrap gap-2">
                            <div>
                              <span className="font-bold text-base">{a.symbol}</span>
                              {a.company_name && (
                                <span className="text-muted-foreground text-sm ml-2">
                                  {a.company_name}
                                </span>
                              )}
                            </div>
                            <RecommendationBadge recommendation={a.recommendation} />
                          </div>
                          <div className="grid grid-cols-3 gap-3 text-sm">
                            <div>
                              <div className="text-xs text-muted-foreground">Current</div>
                              <div className="font-semibold">
                                {formatCurrency(a.current_price)}
                              </div>
                            </div>
                            <div>
                              <div className="text-xs text-muted-foreground">Target</div>
                              <div className="font-semibold text-emerald-500">
                                {formatCurrency(a.target_price)}
                              </div>
                            </div>
                            <div>
                              <div className="text-xs text-muted-foreground">Confidence</div>
                              <div className="font-semibold">{a.confidence}%</div>
                            </div>
                          </div>
                          {a.summary && (
                            <p className="text-sm text-muted-foreground">{a.summary}</p>
                          )}
                          {(a.recommendation === 'BUY' || a.recommendation === 'STRONG_BUY') && (
                            <Button
                              size="sm"
                              onClick={() => executeTrade(a)}
                              className="gap-1.5 w-full sm:w-auto"
                            >
                              <TrendingUp className="w-3.5 h-3.5" />
                              Execute Buy (10 shares)
                            </Button>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </motion.div>
            ))}
          </AnimatePresence>

          {loading && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex gap-3">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent to-primary flex items-center justify-center flex-shrink-0">
                <Bot className="w-4 h-4 text-white" />
              </div>
              <div className="rounded-2xl bg-card border border-border p-4 flex items-center gap-2">
                <Loader2 className="w-4 h-4 animate-spin text-accent" />
                <span className="text-sm text-muted-foreground">Analyzing market data...</span>
              </div>
            </motion.div>
          )}
        </div>
      </div>

      <div className="border-t border-border bg-card/50 backdrop-blur-sm p-4 pb-20 md:pb-4">
        <div className="max-w-4xl mx-auto flex gap-2">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about any stock or market trend..."
            disabled={loading}
            className="flex-1"
          />
          <Button
            onClick={() => sendMessage(input)}
            disabled={loading || !input.trim()}
            className="gap-2"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          </Button>
        </div>
      </div>
    </div>
  );
}