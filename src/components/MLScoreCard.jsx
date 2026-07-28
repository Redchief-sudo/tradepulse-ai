import React from 'react';
import { Radar, RadarChart, PolarGrid, PolarAngleAxis, ResponsiveContainer } from 'recharts';
import { Cpu } from 'lucide-react';
import { cn } from '@/lib/utils';

function scoreColor(score) {
  if (score >= 80) return 'text-emerald-500';
  if (score >= 65) return 'text-green-500';
  if (score >= 45) return 'text-amber-500';
  return 'text-red-500';
}

function scoreBg(score) {
  if (score >= 80) return 'bg-emerald-500';
  if (score >= 65) return 'bg-green-500';
  if (score >= 45) return 'bg-amber-500';
  return 'bg-red-500';
}

const FACTORS = [
  { key: 'technical_score', label: 'Technical', color: '#10b981' },
  { key: 'fundamental_score', label: 'Fundamental', color: '#8b5cf6' },
  { key: 'sentiment_score', label: 'Sentiment', color: '#3b82f6' },
  { key: 'momentum_score', label: 'Momentum', color: '#f59e0b' },
  { key: 'risk_score', label: 'Risk', color: '#ec4899' },
];

export default function MLScoreCard({ scores }) {
  if (!scores || scores.ml_score === undefined) return null;

  const {
    ml_score,
    technical_score,
    fundamental_score,
    sentiment_score,
    momentum_score,
    risk_score,
    ml_signal,
    score_reasoning,
  } = scores;

  const radarData = FACTORS.map((f) => ({
    factor: f.label,
    score: scores[f.key] || 0,
  }));

  return (
    <div className="rounded-xl border border-accent/20 bg-accent/5 p-4 my-3">
      <div className="flex items-center gap-2 mb-3">
        <Cpu className="w-4 h-4 text-accent" />
        <h4 className="text-sm font-semibold">ML Multi-Factor Score</h4>
        <span
          className={cn(
            'ml-auto text-xs font-bold px-2 py-0.5 rounded-full text-white',
            scoreBg(ml_score)
          )}
        >
          {ml_signal?.replace('_', ' ') || '—'}
        </span>
      </div>

      <div className="flex flex-col md:flex-row gap-4">
        {/* Composite score + radar */}
        <div className="flex items-center gap-4 md:w-1/2">
          <div className="text-center flex-shrink-0">
            <div className={cn('text-4xl font-bold', scoreColor(ml_score))}>
              {ml_score.toFixed(0)}
            </div>
            <div className="text-xs text-muted-foreground">Composite</div>
          </div>
          <div className="flex-1 h-[120px]">
            <ResponsiveContainer width="100%" height="100%">
              <RadarChart data={radarData} outerRadius="75%">
                <PolarGrid stroke="#232b3d" />
                <PolarAngleAxis
                  dataKey="factor"
                  tick={{ fill: '#9ca3af', fontSize: 9 }}
                />
                <Radar
                  dataKey="score"
                  stroke="#8b5cf6"
                  fill="#8b5cf6"
                  fillOpacity={0.3}
                  strokeWidth={2}
                />
              </RadarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Factor breakdown bars */}
        <div className="space-y-1.5 md:w-1/2">
          {FACTORS.map((f) => {
            const val = scores[f.key] || 0;
            return (
              <div key={f.key} className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground w-20 flex-shrink-0">
                  {f.label}
                </span>
                <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden">
                  <div
                    className={cn('h-full rounded-full transition-all', scoreBg(val))}
                    style={{ width: `${val}%` }}
                  />
                </div>
                <span className={cn('text-xs font-medium w-8 text-right', scoreColor(val))}>
                  {val.toFixed(0)}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {score_reasoning && (
        <p className="text-xs text-muted-foreground mt-3 leading-relaxed italic">
          {score_reasoning}
        </p>
      )}
    </div>
  );
}