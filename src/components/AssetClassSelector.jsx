import React from 'react';
import { LineChart, Bitcoin, DollarSign, Coins, Landmark } from 'lucide-react';
import { cn } from '@/lib/utils';

const CLASSES = [
  { id: 'stocks', label: 'Stocks', icon: LineChart },
  { id: 'crypto', label: 'Crypto', icon: Bitcoin },
  { id: 'forex', label: 'Forex', icon: DollarSign },
  { id: 'commodities', label: 'Commodities', icon: Coins },
  { id: 'fixed_income', label: 'Fixed Income', icon: Landmark },
];

export default function AssetClassSelector({ value, onChange }) {
  const all = !value || value.length === 0;
  return (
    <div className="flex flex-wrap gap-2 mb-4">
      <button
        onClick={() => onChange([])}
        className={cn(
          'text-xs px-3 py-1.5 rounded-full border transition-all',
          all
            ? 'border-primary bg-primary/15 text-primary'
            : 'border-border text-muted-foreground hover:text-foreground'
        )}
      >
        All Assets
      </button>
      {CLASSES.map((c) => {
        const active = value.includes(c.id);
        return (
          <button
            key={c.id}
            onClick={() => {
              if (active) onChange(value.filter((v) => v !== c.id));
              else onChange([...value, c.id]);
            }}
            className={cn(
              'text-xs px-3 py-1.5 rounded-full border transition-all flex items-center gap-1.5',
              active
                ? 'border-accent bg-accent/15 text-accent'
                : 'border-border text-muted-foreground hover:text-foreground'
            )}
          >
            <c.icon className="w-3.5 h-3.5" />
            {c.label}
          </button>
        );
      })}
    </div>
  );
}