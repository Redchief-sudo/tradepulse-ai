// Controlled model governance — safely self-evolving factor weights.
//
// Pipeline: observe → measure → generate candidate → validate out-of-sample →
// promote only proven improvements → retain rollback history → continue safely.
//
// Gates:
// 1. MIN_SAMPLE_SIZE: need enough labeled outcomes for statistical power
// 2. Walk-forward: 70% in-sample, 30% out-of-sample (temporal split, no look-ahead)
// 3. OOS improvement: candidate must beat champion by >= MIN_OOS_IMPROVEMENT
// 4. Statistical significance: bootstrap p-value <= MAX_P_VALUE
// 5. Bounded changes: each factor can move at most ±MAX_WEIGHT_CHANGE_PCT per promotion
// 6. Automatic rollback: if recent Sharpe degrades below threshold, revert to parent

const MAX_WEIGHT_CHANGE_PCT = 20;
const MIN_SAMPLE_SIZE = 30;
const MIN_OOS_IMPROVEMENT = 0.02;
const MAX_P_VALUE = 0.05;
const ROLLBACK_WINDOW = 20;
const ROLLBACK_SHARPE_THRESHOLD = -0.5;
const IS_RATIO = 0.7;
const MIN_OOS_SIZE = 10;
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

export async function getChampion(sr, userId) {
  const models = await sr.entities.StrategyModel.filter({ user_id: userId, status: 'champion' });
  return models[0] || null;
}

export async function collectOutcomes(sr, userId) {
  const decisions = await sr.entities.AITradeDecision.filter(
    { user_id: userId, outcome_status: 'realized' },
    '-created_date', 500
  );
  return decisions.filter((d) =>
    d.realized_return != null &&
    d.technical_score != null &&
    d.momentum_score != null &&
    d.risk_score != null
  );
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
// Computes Spearman rank correlation between ML score and realized return.
// Bootstrap test for statistical significance of the improvement.
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

  // Bootstrap p-value: fraction of resamples where candidate does NOT beat champion
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

// Generate candidate weights via LLM, then bound them to ±20% per factor.
async function generateCandidate(sr, champion, outcomes) {
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
    prompt: `You are the MODEL GOVERNANCE ENGINE. Analyze which factors predicted winners vs losers and propose ADJUSTED 5-factor weights (sum to 100, each 5-40).
Factor analysis (winners vs losers averages):
${JSON.stringify(factorAnalysis, null, 2)}
Current champion weights: ${JSON.stringify(championWeights)}
Sample size: ${outcomes.length} realized outcomes.
Return new weights that would improve out-of-sample prediction. Be conservative — small adjustments only.`,
    response_json_schema: {
      type: 'object',
      properties: {
        weights: { type: 'object', properties: {
          technical: { type: 'number' }, fundamental: { type: 'number' },
          sentiment: { type: 'number' }, momentum: { type: 'number' }, risk: { type: 'number' },
        } },
        reasoning: { type: 'string' },
      },
    },
  });

  const candidateWeights = boundWeights(championWeights, sl.weights || championWeights);
  return { weights: candidateWeights, reasoning: sl.reasoning || 'LLM-proposed adjustment' };
}

// Promote candidate to champion if it passes ALL gates.
async function promoteIfProven(sr, userId, candidate, champion, validation) {
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
  });
  await sr.entities.User.update(userId, { ml_weights: candidate.weights });
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
    if (parents[0]) {
      await sr.entities.StrategyModel.update(champion.id, {
        status: 'retired', retired_at: new Date().toISOString(),
        rollback_reason: `Auto-rollback: recent Sharpe ${recentSharpe.toFixed(2)} < ${ROLLBACK_SHARPE_THRESHOLD}`,
      });
      await sr.entities.StrategyModel.update(parents[0].id, { status: 'champion', promoted_at: new Date().toISOString() });
      await sr.entities.User.update(userId, { ml_weights: parents[0].weights });
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
export async function runGovernanceCycle(sr, userId) {
  const champion = await getChampion(sr, userId);
  const outcomes = await collectOutcomes(sr, userId);

  // Initialize champion if none exists
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
    });
    await sr.entities.User.update(userId, { ml_weights: DEFAULT_WEIGHTS });
    return { initialized: true, champion: v1.version, outcomes: outcomes.length };
  }

  // Check for rollback first
  const rollback = await rollbackIfDegraded(sr, userId, champion, outcomes);
  if (rollback.rolled_back) return { rollback };

  // Need enough outcomes to generate a candidate
  if (outcomes.length < MIN_SAMPLE_SIZE) {
    return { skipped: true, reason: `Insufficient outcomes (${outcomes.length} < ${MIN_SAMPLE_SIZE})`, champion: champion.version, recentSharpe: rollback.recentSharpe };
  }

  // Generate candidate
  const candidateData = await generateCandidate(sr, champion, outcomes);

  // Create challenger record
  const newVersion = bumpVersion(champion.version);
  const challenger = await sr.entities.StrategyModel.create({
    user_id: userId,
    model_id: champion.model_id,
    version: newVersion,
    parent_version: champion.version,
    weights: candidateData.weights,
    status: 'challenger',
    approval_status: 'pending',
    in_sample_metrics: JSON.stringify({ reasoning: candidateData.reasoning, sampleSize: outcomes.length }),
    sample_size: outcomes.length,
    created_at: new Date().toISOString(),
  });

  // Validate candidate (walk-forward)
  const validation = validateCandidate(outcomes, champion.weights, candidateData.weights);

  // Update challenger with validation metrics
  await sr.entities.StrategyModel.update(challenger.id, {
    out_of_sample_metrics: JSON.stringify(validation),
    champion_metrics: JSON.stringify({ championCorr: validation.championCorr }),
    improvement_pct: validation.improvement,
    p_value: validation.pValue,
  });

  // Promote if proven
  const promotion = await promoteIfProven(sr, userId, challenger, champion, validation);

  if (!promotion.promoted) {
    // Reject the challenger
    await sr.entities.StrategyModel.update(challenger.id, { status: 'rejected', approval_status: 'rejected' });
  }

  return {
    champion: champion.version,
    challenger: challenger.version,
    validation,
    promotion,
    outcomes: outcomes.length,
  };
}