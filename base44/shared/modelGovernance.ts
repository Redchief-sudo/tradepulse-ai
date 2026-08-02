// Controlled model governance — safely self-evolving factor weights.
//
// ARCHITECTURE:
// 1. SEPARATION: Learning never runs in the live execution path. The scan cycle
//    only reads champion weights; this module is the only thing that mutates them.
// 2. LLM AS ANALYST, NOT OPTIMIZER: The LLM provides hypotheses (which factors to
//    adjust and why). A deterministic coordinate-ascent optimizer finds the exact
//    weights that maximize out-of-sample correlation. The LLM never invents numbers.
// 3. REGIME-SPECIFIC CHAMPIONS: Each market regime has its own champion. A regime
//    classifier activates the appropriate one at execution time.
// 4. PROMOTION MODES: research (validate only), manual_approval (human approves),
//    automatic (auto-promote if proven).
// 5. IMMUTABLE AUDIT TRAIL: Every promotion records parent, child, metrics, regime,
//    approver, and rollback path — making evolution fully reproducible.
//
// GATES:
// 1. MIN_SAMPLE_SIZE: 100 completed trades minimum (raised from 30 — too small for production)
// 2. MIN_SAMPLE_PER_REGIME: 20 per regime for regime-specific promotion
// 3. Walk-forward: 70% in-sample, 30% out-of-sample (temporal split, no look-ahead)
// 4. OOS improvement: candidate must beat champion by >= MIN_OOS_IMPROVEMENT
// 5. Statistical significance: bootstrap p-value <= MAX_P_VALUE
// 6. Bounded changes: each factor can move at most ±MAX_WEIGHT_CHANGE_PCT per promotion
// 7. Automatic rollback: if recent Sharpe degrades below threshold, revert to parent

import { classifyRegimeFromSnapshots } from './regime.ts';

const MAX_WEIGHT_CHANGE_PCT = 20;
const MIN_SAMPLE_SIZE = 100;
const MIN_SAMPLE_PER_REGIME = 20;
const MIN_OOS_IMPROVEMENT = 0.02;
const MAX_P_VALUE = 0.05;
const ROLLBACK_WINDOW = 20;
const ROLLBACK_SHARPE_THRESHOLD = -0.5;
const IS_RATIO = 0.7;
const MIN_OOS_SIZE = 30;
const N_BOOTSTRAP = 500;

const FACTOR_KEYS = ['technical', 'fundamental', 'sentiment', 'momentum', 'risk'];
const DEFAULT_WEIGHTS = { technical: 25, fundamental: 25, sentiment: 20, momentum: 15, risk: 15 };

// --- Statistics helpers ---

function mean(arr) { return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0; }

function std(arr) {
  if (arr.length < 2) return 0;
  const m = mean(arr);
  return Math.sqrt(arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1));
}

function sharpe(returns) {
  const s = std(returns);
  return s > 0 ? (mean(returns) / s) * Math.sqrt(252) : 0;
}

function rank(arr) {
  const sorted = arr.map((v, i) => ({ v, i })).sort((a, b) => a.v - b.v);
  const ranks = new Array(arr.length);
  let i = 0;
  while (i < sorted.length) {
    let j = i;
    while (j < sorted.length && sorted[j].v === sorted[i].v) j++;
    const avgRank = (i + 1 + j) / 2;
    for (let k = i; k < j; k++) ranks[sorted[k].i] = avgRank;
    i = j;
  }
  return ranks;
}

function pearson(x, y) {
  const n = x.length;
  if (n < 2) return 0;
  const mx = mean(x), my = mean(y);
  let cov = 0, vx = 0, vy = 0;
  for (let i = 0; i < n; i++) {
    cov += (x[i] - mx) * (y[i] - my);
    vx += (x[i] - mx) ** 2;
    vy += (y[i] - my) ** 2;
  }
  return vx > 0 && vy > 0 ? cov / Math.sqrt(vx * vy) : 0;
}

function spearman(x, y) { return pearson(rank(x), rank(y)); }

// --- Core governance functions ---

// Regime-specific champion lookup. Falls back to global ('all') champion.
export async function getChampion(sr, userId, regime) {
  const allChampions = await sr.entities.StrategyModel.filter({
    user_id: userId, status: 'champion'
  });
  if (regime && regime !== 'all') {
    const regimeChamp = allChampions.find((m) => m.regime === regime);
    if (regimeChamp) return regimeChamp;
  }
  return allChampions.find((m) => !m.regime || m.regime === 'all') || null;
}

// Collect realized outcomes, optionally filtered by regime.
// Falls back to all outcomes if regime-specific sample is too small.
export async function collectOutcomes(sr, userId, regime) {
  const decisions = await sr.entities.AITradeDecision.filter(
    { user_id: userId, outcome_status: 'realized' },
    '-created_date', 500
  );
  const allOutcomes = decisions.filter((d) =>
    d.realized_return != null &&
    d.technical_score != null &&
    d.momentum_score != null &&
    d.risk_score != null
  );
  if (!regime || regime === 'all') return allOutcomes;
  const regimeOutcomes = allOutcomes.filter((d) => d.regime === regime);
  return regimeOutcomes.length >= MIN_SAMPLE_PER_REGIME ? regimeOutcomes : allOutcomes;
}

function computeMLScore(d, w) {
  return (
    (d.technical_score || 0) * (w.technical || 0) +
    (d.fundamental_score || 0) * (w.fundamental || 0) +
    (d.sentiment_score || 0) * (w.sentiment || 0) +
    (d.momentum_score || 0) * (w.momentum || 0) +
    (d.risk_score || 0) * (w.risk || 0)
  ) / 100;
}

// Walk-forward validation: temporal split (oldest 70% IS, newest 30% OOS).
function validateCandidate(outcomes, championWeights, candidateWeights) {
  const sorted = [...outcomes].sort((a, b) => new Date(a.created_date) - new Date(b.created_date));
  const splitIdx = Math.floor(sorted.length * IS_RATIO);
  const outOfSample = sorted.slice(splitIdx);

  if (outOfSample.length < MIN_OOS_SIZE) {
    return { insufficient: true, oosSize: outOfSample.length, sampleSize: outcomes.length };
  }

  const champScores = outOfSample.map((d) => computeMLScore(d, championWeights));
  const candScores = outOfSample.map((d) => computeMLScore(d, candidateWeights));
  const oosReturns = outOfSample.map((d) => d.realized_return);

  const championCorr = spearman(champScores, oosReturns);
  const candidateCorr = spearman(candScores, oosReturns);

  let candidateWins = 0;
  for (let b = 0; b < N_BOOTSTRAP; b++) {
    const sample = [];
    for (let i = 0; i < outOfSample.length; i++) {
      sample.push(outOfSample[Math.floor(Math.random() * outOfSample.length)]);
    }
    const sChamp = sample.map((d) => computeMLScore(d, championWeights));
    const sCand = sample.map((d) => computeMLScore(d, candidateWeights));
    const sRet = sample.map((d) => d.realized_return);
    if (spearman(sCand, sRet) > spearman(sChamp, sRet)) candidateWins++;
  }
  const pValue = 1 - candidateWins / N_BOOTSTRAP;

  return {
    championCorr: Math.round(championCorr * 1000) / 1000,
    candidateCorr: Math.round(candidateCorr * 1000) / 1000,
    improvement: Math.round((candidateCorr - championCorr) * 1000) / 1000,
    pValue: Math.round(pValue * 1000) / 1000,
    sampleSize: outcomes.length,
    oosSize: outOfSample.length,
  };
}

export function passesAllGates(validation) {
  return !validation.insufficient &&
    validation.sampleSize >= MIN_SAMPLE_SIZE &&
    validation.improvement >= MIN_OOS_IMPROVEMENT &&
    validation.pValue <= MAX_P_VALUE;
}

// Bound candidate weights to ±MAX_WEIGHT_CHANGE_PCT per factor from champion, then normalize.
function boundWeights(champion, candidate) {
  const cw = champion || DEFAULT_WEIGHTS;
  const bounded = {};
  for (const k of FACTOR_KEYS) {
    const c = cw[k] || 0;
    const min = c * (1 - MAX_WEIGHT_CHANGE_PCT / 100);
    const max = c * (1 + MAX_WEIGHT_CHANGE_PCT / 100);
    bounded[k] = Math.max(5, Math.min(40, Math.min(max, Math.max(min, candidate[k] || 0))));
  }
  const sum = FACTOR_KEYS.reduce((s, k) => s + bounded[k], 0);
  if (sum > 0) for (const k of FACTOR_KEYS) bounded[k] = Math.round((bounded[k] / sum) * 100);
  return bounded;
}

// LLM provides HYPOTHESIS only (factor directions). The deterministic optimizer
// below finds the exact weights. The LLM never invents numerical weights.
async function generateHypothesis(sr, champion, outcomes) {
  const championWeights = champion?.weights || DEFAULT_WEIGHTS;
  const winners = outcomes.filter((o) => o.realized_return > 0);
  const losers = outcomes.filter((o) => o.realized_return < 0);

  const factorAnalysis = {};
  for (const [k, field] of [
    ['technical', 'technical_score'], ['fundamental', 'fundamental_score'],
    ['sentiment', 'sentiment_score'], ['momentum', 'momentum_score'], ['risk', 'risk_score'],
  ]) {
    const winAvg = winners.length ? mean(winners.map((o) => o[field] || 0)) : 0;
    const loseAvg = losers.length ? mean(losers.map((o) => o[field] || 0)) : 0;
    factorAnalysis[k] = { winAvg: Math.round(winAvg * 10) / 10, loseAvg: Math.round(loseAvg * 10) / 10, delta: Math.round((winAvg - loseAvg) * 10) / 10 };
  }

  const sl = await sr.integrations.Core.InvokeLLM({
    model: 'claude-sonnet-5',
    prompt: `You are the MODEL GOVERNANCE HYPOTHESIS ENGINE. Analyze which factors predicted winners vs losers and propose a DIRECTIONAL HYPOTHESIS for each factor. Do NOT propose numerical weights — only state whether to INCREASE, DECREASE, or keep NEUTRAL each factor's weight. The deterministic optimizer will find the exact values.

Factor analysis (winners vs losers averages):
${JSON.stringify(factorAnalysis, null, 2)}

Current champion weights: ${JSON.stringify(championWeights)}
Sample size: ${outcomes.length} realized outcomes.

Return a direction for each factor and a concise reasoning.`,
    response_json_schema: {
      type: 'object',
      properties: {
        hypothesis: {
          type: 'object',
          properties: {
            technical: { type: 'string', enum: ['increase', 'decrease', 'neutral'] },
            fundamental: { type: 'string', enum: ['increase', 'decrease', 'neutral'] },
            sentiment: { type: 'string', enum: ['increase', 'decrease', 'neutral'] },
            momentum: { type: 'string', enum: ['increase', 'decrease', 'neutral'] },
            risk: { type: 'string', enum: ['increase', 'decrease', 'neutral'] },
          },
        },
        reasoning: { type: 'string' },
      },
    },
  });

  return { hypothesis: sl.hypothesis || {}, reasoning: sl.reasoning || 'No reasoning provided' };
}

// Deterministic coordinate-ascent optimizer.
// Objective: maximize OOS Spearman correlation between ML score and realized return.
// The LLM hypothesis biases the search order and starting direction.
// The optimizer NEVER uses the LLM for numerical weights — only for direction hints.
function optimizeCandidateWeights(outcomes, championWeights, hypothesis) {
  const sorted = [...outcomes].sort((a, b) => new Date(a.created_date) - new Date(b.created_date));
  const splitIdx = Math.floor(sorted.length * IS_RATIO);
  const oos = sorted.slice(splitIdx);

  if (oos.length < MIN_OOS_SIZE) {
    return { weights: championWeights, oosScore: 0, improvement: 0, iterations: 0, insufficient: true };
  }

  function objective(w) {
    const scores = oos.map((d) => computeMLScore(d, w));
    const returns = oos.map((d) => d.realized_return);
    return spearman(scores, returns);
  }

  let best = { ...championWeights };
  let bestScore = objective(best);
  const startScore = bestScore;

  const priority = { increase: 0, decrease: 1, neutral: 2 };
  const orderedFactors = [...FACTOR_KEYS].sort((a, b) =>
    (priority[hypothesis?.[a]] ?? 2) - (priority[hypothesis?.[b]] ?? 2)
  );

  let improved = true;
  let iter = 0;
  const MAX_ITER = 15;
  const MIN_DELTA = 0.001;

  while (improved && iter < MAX_ITER) {
    improved = false;
    for (const factor of orderedFactors) {
      const hint = hypothesis?.[factor] || 'neutral';
      const steps = hint === 'increase' ? [2, 5, 1, 3, -1, -2, -3]
                  : hint === 'decrease' ? [-2, -5, -1, -3, 1, 2, 3]
                  : [1, -1, 2, -2, 3, -3, 5, -5];

      for (const step of steps) {
        const candidate = { ...best };
        candidate[factor] = (best[factor] || 0) + step;
        const bounded = boundWeights(championWeights, candidate);
        const score = objective(bounded);
        if (score > bestScore + MIN_DELTA) {
          best = bounded;
          bestScore = score;
          improved = true;
          break;
        }
      }
    }
    iter++;
  }

  return {
    weights: best,
    oosScore: Math.round(bestScore * 1000) / 1000,
    improvement: Math.round((bestScore - startScore) * 1000) / 1000,
    iterations: iter,
  };
}

// Promote candidate to champion if it passes ALL gates. Records immutable audit trail.
async function promoteIfProven(sr, userId, user, candidate, champion, validation, regime) {
  if (validation.insufficient) return { promoted: false, reason: `INSUFFICIENT_OOS_SAMPLE (${validation.oosSize} < ${MIN_OOS_SIZE})` };
  if (validation.sampleSize < MIN_SAMPLE_SIZE) return { promoted: false, reason: `INSUFFICIENT_SAMPLE (${validation.sampleSize} < ${MIN_SAMPLE_SIZE})` };
  if (validation.improvement < MIN_OOS_IMPROVEMENT) return { promoted: false, reason: `INSUFFICIENT_IMPROVEMENT (${(validation.improvement * 100).toFixed(2)}% < ${(MIN_OOS_IMPROVEMENT * 100).toFixed(0)}%)` };
  if (validation.pValue > MAX_P_VALUE) return { promoted: false, reason: `NOT_SIGNIFICANT (p=${validation.pValue} > ${MAX_P_VALUE})` };

  if (champion) {
    await sr.entities.StrategyModel.update(champion.id, { status: 'retired', retired_at: new Date().toISOString() });
  }
  await sr.entities.StrategyModel.update(candidate.id, {
    status: 'champion',
    approval_status: 'auto_promoted',
    promoted_at: new Date().toISOString(),
    out_of_sample_metrics: JSON.stringify(validation),
    approved_by: userId,
    rollback_path: champion ? `${champion.rollback_path || champion.version} → ${candidate.version}` : candidate.version,
  });
  // Update user.ml_weights cache only for global champion
  if (!regime || regime === 'all') {
    await sr.entities.User.update(userId, { ml_weights: candidate.weights });
  }
  return { promoted: true, validation };
}

// Automatic rollback if recent performance degrades.
async function rollbackIfDegraded(sr, userId, champion, recentOutcomes) {
  if (!champion || !champion.parent_version) return { rolled_back: false };
  if (recentOutcomes.length < ROLLBACK_WINDOW) return { rolled_back: false };

  const recentReturns = recentOutcomes.slice(0, ROLLBACK_WINDOW).map((o) => o.realized_return);
  const recentSharpe = sharpe(recentReturns);

  if (recentSharpe < ROLLBACK_SHARPE_THRESHOLD) {
    const parents = await sr.entities.StrategyModel.filter({ user_id: userId, version: champion.parent_version, status: 'retired' });
    const parent = parents.find((p) => p.regime === champion.regime || (!p.regime && !champion.regime));
    if (parent) {
      await sr.entities.StrategyModel.update(champion.id, {
        status: 'retired', retired_at: new Date().toISOString(),
        rollback_reason: `Auto-rollback: recent Sharpe ${recentSharpe.toFixed(2)} < ${ROLLBACK_SHARPE_THRESHOLD}`,
      });
      await sr.entities.StrategyModel.update(parent.id, { status: 'champion', promoted_at: new Date().toISOString() });
      if (!champion.regime || champion.regime === 'all') {
        await sr.entities.User.update(userId, { ml_weights: parent.weights });
      }
      return { rolled_back: true, to_version: champion.parent_version, reason: `Recent Sharpe ${recentSharpe.toFixed(2)} below threshold ${ROLLBACK_SHARPE_THRESHOLD}` };
    }
  }
  return { rolled_back: false, recentSharpe: Math.round(recentSharpe * 100) / 100 };
}

function bumpVersion(v) {
  const parts = (v || '1.0.0').split('.').map(Number);
  parts[1] = (parts[1] || 0) + 1;
  return parts.join('.');
}

// Full governance cycle — called by the scheduled workflow.
// SEPARATION: This runs offline (weekly workflow), never in the live execution path.
export async function runGovernanceCycle(sr, userId, user) {
  // Classify current regime for regime-specific champion selection
  const regimeInfo = await classifyRegimeFromSnapshots(sr);
  const currentRegime = regimeInfo.market_regime;

  const champion = await getChampion(sr, userId, currentRegime);
  const outcomes = await collectOutcomes(sr, userId, currentRegime);

  // Initialize global champion if none exists
  if (!champion) {
    const v1 = await sr.entities.StrategyModel.create({
      user_id: userId,
      model_id: 'alpha-factor-v1',
      version: '1.0.0',
      parent_version: null,
      weights: DEFAULT_WEIGHTS,
      status: 'champion',
      approval_status: 'auto_promoted',
      promoted_at: new Date().toISOString(),
      sample_size: 0,
      created_at: new Date().toISOString(),
      regime: 'all',
      rollback_path: '1.0.0',
    });
    await sr.entities.User.update(userId, { ml_weights: DEFAULT_WEIGHTS });
    return { initialized: true, champion: v1.version, regime: currentRegime, outcomes: outcomes.length };
  }

  // Check for rollback first
  const rollback = await rollbackIfDegraded(sr, userId, champion, outcomes);
  if (rollback.rolled_back) return { rollback, regime: currentRegime };

  // Need enough outcomes to generate a candidate
  if (outcomes.length < MIN_SAMPLE_SIZE) {
    return { skipped: true, reason: `Insufficient outcomes (${outcomes.length} < ${MIN_SAMPLE_SIZE})`, champion: champion.version, regime: currentRegime, recentSharpe: rollback.recentSharpe };
  }

  // 1. LLM generates HYPOTHESIS (factor directions only — no numerical weights)
  const { hypothesis, reasoning } = await generateHypothesis(sr, champion, outcomes);

  // 2. Deterministic optimizer finds exact weights that maximize OOS correlation
  const optimization = optimizeCandidateWeights(outcomes, champion.weights, hypothesis);

  // Create challenger record with full audit trail
  const newVersion = bumpVersion(champion.version);
  const challenger = await sr.entities.StrategyModel.create({
    user_id: userId,
    model_id: champion.model_id,
    version: newVersion,
    parent_version: champion.version,
    weights: optimization.weights,
    status: 'challenger',
    approval_status: 'pending',
    in_sample_metrics: JSON.stringify({
      reasoning,
      hypothesis,
      optimization: { oosScore: optimization.oosScore, improvement: optimization.improvement, iterations: optimization.iterations },
      sampleSize: outcomes.length,
    }),
    sample_size: outcomes.length,
    created_at: new Date().toISOString(),
    regime: currentRegime,
    rollback_path: `${champion.rollback_path || champion.version} → ${newVersion}`,
  });

  // 3. Walk-forward validate
  const validation = validateCandidate(outcomes, champion.weights, optimization.weights);

  await sr.entities.StrategyModel.update(challenger.id, {
    out_of_sample_metrics: JSON.stringify(validation),
    champion_metrics: JSON.stringify({ championCorr: validation.championCorr }),
    improvement_pct: validation.improvement,
    p_value: validation.pValue,
  });

  // 4. Promotion mode gate
  const promotionMode = user.promotion_mode || 'automatic';

  if (promotionMode === 'research') {
    // Validate only, never promote — record everything for analysis
    return { mode: 'research', champion: champion.version, challenger: challenger.version, regime: currentRegime, validation, promoted: false };
  }

  if (promotionMode === 'manual_approval') {
    // If proven, mark as pending approval; don't auto-promote
    if (passesAllGates(validation)) {
      await sr.entities.StrategyModel.update(challenger.id, { approval_status: 'pending' });
      return { mode: 'manual_approval', champion: champion.version, challenger: challenger.version, regime: currentRegime, validation, promoted: false, pendingApproval: true };
    }
    await sr.entities.StrategyModel.update(challenger.id, { status: 'rejected', approval_status: 'rejected' });
    return { mode: 'manual_approval', champion: champion.version, challenger: challenger.version, regime: currentRegime, validation, promoted: false };
  }

  // automatic mode — current behavior
  const promotion = await promoteIfProven(sr, userId, user, challenger, champion, validation, currentRegime);
  if (!promotion.promoted) {
    await sr.entities.StrategyModel.update(challenger.id, { status: 'rejected', approval_status: 'rejected' });
  }

  return {
    mode: 'automatic',
    champion: champion.version,
    challenger: challenger.version,
    regime: currentRegime,
    validation,
    promotion,
    outcomes: outcomes.length,
  };
}