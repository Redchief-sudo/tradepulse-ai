import React, { useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { motion } from 'framer-motion';
import { Eye, Plus, Trash2, RefreshCw, Loader2, Target, Bot, Bitcoin, LineChart } from 'lucide-react';
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

const CRYPTO_NAMES = {
  BTC: 'Bitcoin',
  ETH: 'Ethereum',
  SOL: 'Solana',
  ADA: 'Cardano',
  DOGE: 'Dogecoin',
  XRP: 'XRP',
  DOT: 'Polkadot',
  AVAX: 'Avalanche',
  MATIC: 'Polygon',
  LINK: 'Chainlink',
  LTC: 'Litecoin',
  BCH: 'Bitcoin Cash',
  UNI: 'Uniswap',
  ATOM: 'Cosmos',
  ALGO: 'Algorand',
};

function AssetClassBadge({ assetClass }) {
  const isCrypto = (assetClass || 'stocks') === 'crypto';
  return (
    <span
      className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
        isCrypto
          ? 'bg-amber-500/15 text-amber-400'
          : 'bg-primary/15 text-primary'
      }`}
    >
      {isCrypto ? 'CRYPTO' : 'STOCK'}
    </span>
  );
}

export default function Watchlist() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [open, setOpen] = useState(false);
  const [newSymbol, setNewSymbol] = useState('');
  const [newAssetClass, setNewAssetClass] = useState('stocks');
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
      const quoteItems = items.map((i) => ({
        symbol: i.symbol,
        asset_class: i.asset_class || 'stocks',
      }));
      const result = await base44.functions.invoke('getMultiAssetQuotes', { items: quoteItems });
      const quotes = (result.data?.quotes || []).filter((q) => !q.error && q.price > 0);
      const priceMap = {};
      quotes.forEach((q) => {
        priceMap[q.symbol.toUpperCase()] = q;
      });
      const updates = items
        .filter((i) => priceMap[i.symbol])
        .map((i) => ({
          id: i.id,
          current_price: priceMap[i.symbol].price,
          day_change_percent: priceMap[i.symbol].day_change_percent,
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
      const sym = newSymbol.toUpperCase().trim();
      const assetClass = newAssetClass;
      let companyName = '';
      let currentPrice = 0;
      let dayChange = 0;

      if (assetClass === 'crypto') {
        const result = await base44.functions.invoke('getMultiAssetQuotes', {
          items: [{ symbol: sym, asset_class: 'crypto' }],
        });
        const q = (result.data?.quotes || [])[0];
        if (q && !q.error && q.price > 0) {
          currentPrice = q.price;
          dayChange = q.day_change_percent;
        }
        companyName = CRYPTO_NAMES[sym] || sym;
      } else {
        const result = await base44.functions.invoke('marketData', {
          symbols: [sym],
          include_fundamentals: true,
        });
        const q = (result.data?.quotes || [])[0];
        if (q && !q.error) {
          currentPrice = q.current_price || 0;
          dayChange = q.day_change_percent || 0;
          companyName = q.fundamentals?.name || '';
        }
      }

      await base44.entities.WatchlistItem.create({
        symbol: sym,
        company_name: companyName,
        current_price: currentPrice,
        day_change_percent: dayChange,
        target_price: 0,
        notes: '',
        asset_class: assetClass,
      });
      setNewSymbol('');
      setNewAssetClass('stocks');
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

  const analyzeAsset = async (item) => {
    const symbol = item.symbol;
    const assetClass = item.asset_class || 'stocks';
    setAnalyzing(symbol);
    setAnalysisResult(null);
    try {
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Analyze ${assetClass === 'crypto' ? 'cryptocurrency' : 'stock'} ${symbol}. Current price is $${item.current_price || 0}. Provide a recommendation (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL), confidence score (0-100), target price, and a concise analysis summary. Use real-time market data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
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

      if (item) {
        await base44.entities.WatchlistItem.update(item.id, {
          target_price: result.target_price,
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
          <p className="text-muted-foreground text-sm mt-1">
            Track stocks and crypto with live prices and AI analysis
          </p>
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
          <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) setNewAssetClass('stocks'); }}>
            <DialogTrigger asChild>
              <Button className="gap-2">
                <Plus className="w-4 h-4" /> Add Asset
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-card border-border">
              <DialogHeader>
                <DialogTitle>Add to Watchlist</DialogTitle>
              </DialogHeader>
              <div className="space-y-3">
                <div>
                  <Label>Asset Class</Label>
                  <div className="flex gap-2 mt-2">
                    <button
                      type="button"
                      onClick={() => setNewAssetClass('stocks')}
                      className={`flex-1 flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
                        newAssetClass === 'stocks'
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-border text-muted-foreground hover:bg-muted/50'
                      }`}
                    >
                      <LineChart className="w-4 h-4" /> Stock
                    </button>
                    <button
                      type="button"
                      onClick={() => setNewAssetClass('crypto')}
                      className={`flex-1 flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
                        newAssetClass === 'crypto'
                          ? 'border-amber-500 bg-amber-500/10 text-amber-400'
                          : 'border-border text-muted-foreground hover:bg-muted/50'
                      }`}
                    >
                      <Bitcoin className="w-4 h-4" /> Crypto
                    </button>
                  </div>
                </div>
                <div>
                  <Label>Symbol</Label>
                  <Input
                    value={newSymbol}
                    onChange={(e) => setNewSymbol(e.target.value)}
                    placeholder={newAssetClass === 'crypto' ? 'BTC' : 'AAPL'}
                    className="uppercase"
                    onKeyDown={(e) => e.key === 'Enter' && addItem()}
                  />
                  <p className="text-xs text-muted-foreground mt-2">
                    {newAssetClass === 'crypto'
                      ? 'Live price from Coinbase. Common coins: BTC, ETH, SOL, ADA, DOGE.'
                      : 'Live price and company name from Finnhub.'}
                  </p>
                </div>
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
          <h3 className="text-lg font-semibold mb-2">No assets on your watchlist</h3>
          <p className="text-muted-foreground text-sm mb-6 max-w-md mx-auto">
            Add stocks or crypto to track with live prices and AI-powered analysis.
          </p>
          <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) setNewAssetClass('stocks'); }}>
            <DialogTrigger asChild>
              <Button className="gap-2">
                <Plus className="w-4 h-4" /> Add Asset
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
                      <AssetClassBadge assetClass={item.asset_class} />
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
                  onClick={() => analyzeAsset(item)}
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