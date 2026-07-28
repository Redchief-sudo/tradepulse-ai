import React from 'react';
import RecommendationBadge from '@/components/RecommendationBadge';

export default function CandidateCard({ candidate, index }) {
  const c = candidate;
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div>
          <span className="font-bold">{c.symbol}</span>
          <span className="text-muted-foreground text-sm ml-2">{c.company_name}</span>
          {c.sector && (
            <span className="text-xs text-muted-foreground ml-2">· {c.sector}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {c.rsi > 0 && (
            <span className="text-xs text-muted-foreground">RSI {c.rsi.toFixed(0)}</span>
          )}
          {c.macd_signal && (
            <span
              className={`text-xs px-1.5 py-0.5 rounded ${
                c.macd_signal === 'bullish'
                  ? 'bg-emerald-500/15 text-emerald-400'
                  : c.macd_signal === 'bearish'
                  ? 'bg-red-500/15 text-red-400'
                  : 'bg-muted text-muted-foreground'
              }`}
            >
              MACD {c.macd_signal}
            </span>
          )}
          <RecommendationBadge recommendation={c.recommendation} />
        </div>
      </div>
      <div className="text-sm font-medium mb-2">
        {new Intl.NumberFormat('en-US', {
          style: 'currency',
          currency: 'USD',
          minimumFractionDigits: 2,
        }).format(c.current_price)}{' '}
        · Target{' '}
        {new Intl.NumberFormat('en-US', {
          style: 'currency',
          currency: 'USD',
          minimumFractionDigits: 2,
        }).format(c.target_price)}
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground mb-2">
        {c.ma50 > 0 && c.ma200 > 0 && (
          <div>
            MA50: {c.ma50.toFixed(2)} / MA200: {c.ma200.toFixed(2)}{' '}
            <span className={c.ma50 > c.ma200 ? 'text-emerald-500' : 'text-red-500'}>
              {c.ma50 > c.ma200 ? '↗ Golden' : '↘ Death'}
            </span>
          </div>
        )}
        {c.bollinger_position && (
          <div>Bollinger: {c.bollinger_position}</div>
        )}
        {c.volume_trend && <div>Volume: {c.volume_trend}</div>}
        {c.support_level > 0 && <div>Support: {c.support_level.toFixed(2)}</div>}
        {c.resistance_level > 0 && <div>Resistance: {c.resistance_level.toFixed(2)}</div>}
      </div>
      {c.fundamentals && (
        <p className="text-xs text-muted-foreground mb-1">
          <span className="font-medium text-foreground/70">Fundamentals:</span> {c.fundamentals}
        </p>
      )}
      {c.technicals && (
        <p className="text-xs text-muted-foreground mb-1">
          <span className="font-medium text-foreground/70">Technicals:</span> {c.technicals}
        </p>
      )}
      {c.news_catalysts && (
        <p className="text-xs text-muted-foreground mb-1">
          <span className="font-medium text-foreground/70">News:</span> {c.news_catalysts}
        </p>
      )}
      {c.summary && <p className="text-xs text-muted-foreground mt-2 italic">{c.summary}</p>}
    </div>
  );
}