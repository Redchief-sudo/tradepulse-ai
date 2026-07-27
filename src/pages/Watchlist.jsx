import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Eye, Plus, Trash2, RefreshCw, Loader2, Target, Bot } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';

function formatCurrency(n) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
  }).format(n || 0);
}

const recConfig = {
  STRONG_BUY: 'text-emerald-400',
  BUY: 'text-green-400',
  HOLD: 'text-amber-400',
  SELL: 'text-orange-400',
  STRONG_SELL: 'text-red-400',
};

export default function Watchlist() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [open, setOpen] = useState(false);
  const [newSymbol, setNewSymbol] = useState('');
  const [analyzing, setAnalyzing] = useState(null);
  const [analysisResult, setAnalysisResult] = useState(null);
  const [adding, setAdding] = useState(false);

  const loadData = useCallback(async () => {
    try {
      const w = await base44.entities.WatchlistItem.list();
      setItems(w || []);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const refreshPrices = async () => {
    if (items.length === 0) return;
    setRefreshing(true);
    try {
      const symbols = items.map((i) => i.symbol).join(', ');
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Get current stock prices and today's percentage change for these tickers: ${symbols}. Return accurate real-time data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
            stocks: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  symbol: { type: 'string' },
                  current_price: { type: 'number' },
                  day_change_percent: { type: 'number' },
                },
              },
            },
          },
        },
      });
      const stockData = (result.stocks || []).reduce((acc, s) => {
        acc[s.symbol.toUpperCase()] = s;
        return acc;
      }, {});
      const updates = items
        .filter((i) => stockData[i.symbol])
        .map((i) => ({
          id: i.id,
          current_price: stockData[i.symbol].current_price,
          day_change_percent: stockData[i.symbol].day_change_percent,
        }));
      if (updates.length) {
        await base44.entities.WatchlistItem.bulkUpdate(updates);
        await loadData();
      }
    } catch (e) {
      console.error(e);
    }
    setRefreshing(false);
  };

  const addItem = async () => {
    if (!newSymbol.trim()) return;
    setAdding(true);
    try {
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Look up stock ticker "${newSymbol}". Return company name, current stock price, and today's percent change. Use real-time data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
            company_name: { type: 'string' },
            current_price: { type: 'number' },
            day_change_percent: { type: 'number' },
          },
        },
      });
      await base44.entities.WatchlistItem.create({
        symbol: newSymbol.toUpperCase(),
        company_name: result.company_name || '',
        current_price: result.current_price || 0,
        day_change_percent: result.day_change_percent || 0,
        target_price: 0,
        notes: '',
      });
      setNewSymbol('');
      setOpen(false);
      await loadData();
    } catch (e) {
      console.error(e);
    }
    setAdding(false);
  };

  const removeItem = async (id) => {
    await base44.entities.WatchlistItem.delete(id);
    await loadData();
  };

  const analyzeStock = async (symbol) => {
    setAnalyzing(symbol);
    setAnalysisResult(null);
    try {
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Analyze the stock ${symbol}. Provide current price, recommendation (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL), confidence score (0-100), target price, and a concise analysis summary. Use real-time market data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
            current_price: { type: 'number' },
            recommendation: {
              type: 'string',
              enum: ['STRONG_BUY', 'BUY', 'HOLD', 'SELL', 'STRONG_SELL'],
            },
            confidence: { type: 'number' },
            target_price: { type: 'number' },
            summary: { type: 'string' },
          },
        },
      });
      setAnalysisResult({ symbol, ...result });

      const item = items.find((i) => i.symbol === symbol);
      if (item) {
        await base44.entities.WatchlistItem.update(item.id, {
          target_price: result.target_price,
          current_price: result.current_price,
        });
        await loadData();
      }
    } catch (e) {
      console.error(e);
    }
    setAnalyzing(null);
  };

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
          <h1 className="text-2xl md:text-3xl font-bold font-heading tracking-tight">Watchlist</h1>
          <p className="text-muted-foreground text-sm mt-1">Track stocks and get AI analysis</p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={refreshPrices}
            disabled={refreshing || items.length === 0}
            className="gap-2"
          >
            {refreshing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            Refresh
          </Button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button className="gap-2">
                <Plus className="w-4 h-4" /> Add Stock
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-card border-border">
              <DialogHeader>
                <DialogTitle>Add to Watchlist</DialogTitle>
              </DialogHeader>
              <div className="space-y-2">
                <Label>Stock Symbol</Label>
                <Input
                  value={newSymbol}
                  onChange={(e) => setNewSymbol(e.target.value)}
                  placeholder="AAPL"
                  className="uppercase"
                  onKeyDown={(e) => e.key === 'Enter' && addItem()}
                />
                <p className="text-xs text-muted-foreground">
                  AI will look up the company name and current price automatically.
                </p>
              </div>
              <DialogFooter>
                <Button
                  onClick={addItem}
                  disabled={adding || !newSymbol.trim()}
                  className="gap-2 w-full"
                >
                  {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                  Add
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {items.length === 0 ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="rounded-2xl border border-border bg-card p-12 text-center"
        >
          <Eye className="w-12 h-12 mx-auto text-muted-foreground mb-4" />
          <h3 className="text-lg font-semibold mb-2">No stocks on your watchlist</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            Add stocks to track and get AI-powered analysis with price targets.
          </p>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button className="gap-2">
                <Plus className="w-4 h-4" /> Add Stock
              </Button>
            </DialogTrigger>
          </Dialog>
        </motion.div>
      ) : (
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map((item, i) => {
            const isAnalyzing = analyzing === item.symbol;
            const result =
              analysisResult?.symbol === item.symbol ? analysisResult : null;
            const upside =
              item.target_price && item.current_price
                ? ((item.target_price - item.current_price) / item.current_price) * 100
                : 0;

            return (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                className="rounded-2xl border border-border bg-card p-5 space-y-4"
              >
                <div className="flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-lg">{item.symbol}</span>
                      <span
                        className={`text-xs ${
                          (item.day_change_percent || 0) >= 0 ? 'text-emerald-500' : 'text-red-500'
                        }`}
                      >
                        {(item.day_change_percent || 0) >= 0 ? '+' : ''}
                        {(item.day_change_percent || 0).toFixed(2)}%
                      </span>
                    </div>
                    {item.company_name && (
                      <p className="text-xs text-muted-foreground truncate max-w-[180px]">
                        {item.company_name}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={() => removeItem(item.id)}
                    className="text-muted-foreground hover:text-red-500 transition-colors"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                <div className="text-2xl font-bold">{formatCurrency(item.current_price)}</div>

                {item.target_price > 0 && (
                  <div className="flex items-center gap-2 text-sm">
                    <Target className="w-3.5 h-3.5 text-muted-foreground" />
                    <span className="text-muted-foreground">Target:</span>
                    <span className="font-medium text-emerald-500">
                      {formatCurrency(item.target_price)}
                    </span>
                    <span
                      className={upside >= 0 ? 'text-emerald-500 text-xs' : 'text-red-500 text-xs'}
                    >
                      ({upside >= 0 ? '+' : ''}
                      {upside.toFixed(1)}%)
                    </span>
                  </div>
                )}

                {result && (
                  <div className="rounded-lg bg-muted/50 p-3 space-y-2 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground text-xs">AI Recommendation</span>
                      <span
                        className={`font-bold ${
                          recConfig[result.recommendation] || 'text-muted-foreground'
                        }`}
                      >
                        {result.recommendation?.replace('_', ' ')}
                      </span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground text-xs">Confidence</span>
                      <span className="font-medium">{result.confidence}%</span>
                    </div>
                    {result.summary && (
                      <p className="text-xs text-muted-foreground leading-relaxed pt-1">
                        {result.summary}
                      </p>
                    )}
                  </div>
                )}

                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => analyzeStock(item.symbol)}
                  disabled={isAnalyzing}
                  className="w-full gap-2"
                >
                  {isAnalyzing ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Bot className="w-3.5 h-3.5" />
                  )}
                  {isAnalyzing ? 'Analyzing...' : 'AI Analysis'}
                </Button>
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}