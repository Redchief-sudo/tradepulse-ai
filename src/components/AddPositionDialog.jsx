import React, { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Plus, Loader2, Search } from 'lucide-react';
import { base44 } from '@/api/base44Client';

export default function AddPositionDialog({ onAdd }) {
  const [open, setOpen] = useState(false);
  const [symbol, setSymbol] = useState('');
  const [companyName, setCompanyName] = useState('');
  const [shares, setShares] = useState('');
  const [avgPrice, setAvgPrice] = useState('');
  const [sector, setSector] = useState('');
  const [loading, setLoading] = useState(false);
  const [lookingUp, setLookingUp] = useState(false);

  const handleLookup = async () => {
    if (!symbol.trim()) return;
    setLookingUp(true);
    try {
      const result = await base44.integrations.Core.InvokeLLM({
        prompt: `Look up the stock with ticker symbol "${symbol}". Return the company name, current stock price, and sector. Use real-time data.`,
        add_context_from_internet: true,
        model: 'gemini_3_flash',
        response_json_schema: {
          type: 'object',
          properties: {
            company_name: { type: 'string' },
            current_price: { type: 'number' },
            sector: { type: 'string' },
          },
        },
      });
      setCompanyName(result.company_name || '');
      setAvgPrice(result.current_price?.toString() || '');
      setSector(result.sector || '');
    } catch (e) {
      console.error('Lookup failed', e);
    }
    setLookingUp(false);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!symbol || !shares || !avgPrice) return;
    setLoading(true);
    try {
      await onAdd({
        symbol: symbol.toUpperCase(),
        company_name: companyName,
        shares: parseFloat(shares),
        avg_price: parseFloat(avgPrice),
        current_price: parseFloat(avgPrice),
        sector,
        day_change_percent: 0,
      });
      setSymbol('');
      setCompanyName('');
      setShares('');
      setAvgPrice('');
      setSector('');
      setOpen(false);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="gap-2">
          <Plus className="w-4 h-4" /> Add Position
        </Button>
      </DialogTrigger>
      <DialogContent className="bg-card border-border">
        <DialogHeader>
          <DialogTitle>Add Stock Position</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label>Symbol</Label>
            <div className="flex gap-2">
              <Input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="AAPL"
                className="uppercase"
              />
              <Button
                type="button"
                variant="secondary"
                onClick={handleLookup}
                disabled={!symbol.trim() || lookingUp}
                className="gap-1.5"
              >
                {lookingUp ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                Look up
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            <Label>Company Name</Label>
            <Input
              value={companyName}
              onChange={(e) => setCompanyName(e.target.value)}
              placeholder="Apple Inc."
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label>Shares</Label>
              <Input
                type="number"
                step="any"
                value={shares}
                onChange={(e) => setShares(e.target.value)}
                placeholder="10"
                required
              />
            </div>
            <div className="space-y-2">
              <Label>Avg Price ($)</Label>
              <Input
                type="number"
                step="any"
                value={avgPrice}
                onChange={(e) => setAvgPrice(e.target.value)}
                placeholder="150.00"
                required
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label>Sector</Label>
            <Input value={sector} onChange={(e) => setSector(e.target.value)} placeholder="Technology" />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={loading} className="gap-2 w-full">
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              Add Position
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}