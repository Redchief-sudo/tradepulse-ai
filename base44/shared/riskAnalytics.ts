// Deterministic portfolio risk analytics computed from historical price series.
// All formulas are standard quantitative finance — no LLM estimations.

export function mean(arr) {
  return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
}

export function std(arr) {
  if (arr.length < 2) return 0;
  const m = mean(arr);
  const variance = arr.reduce((s, v) => s + (v - m) ** 2, 0) / (arr.length - 1);
  return Math.sqrt(variance);
}

export function correlation(arrX, arrY) {
  if (arrX.length !== arrY.length || arrX.length < 2) return 0;
  const mx = mean(arrX), my = mean(arrY);
  let cov = 0, varX = 0, varY = 0;
  for (let i = 0; i < arrX.length; i++) {
    const dx = arrX[i] - mx, dy = arrY[i] - my;
    cov += dx * dy; varX += dx * dx; varY += dy * dy;
  }
  return varX > 0 && varY > 0 ? cov / Math.sqrt(varX * varY) : 0;
}

// CAPM beta: cov(stock, bench) / var(bench)
export function beta(stockReturns, benchReturns) {
  if (stockReturns.length !== benchReturns.length || stockReturns.length < 2) return 0;
  const mb = mean(benchReturns);
  const ms = mean(stockReturns);
  let cov = 0, benchVar = 0;
  for (let i = 0; i < stockReturns.length; i++) {
    cov += (stockReturns[i] - ms) * (benchReturns[i] - mb);
    benchVar += (benchReturns[i] - mb) ** 2;
  }
  return benchVar > 0 ? cov / benchVar : 0;
}

// Weighted portfolio daily returns from aligned asset return arrays.
export function portfolioDailyReturns(assetReturns, weights) {
  const n = assetReturns[0]?.length || 0;
  if (n === 0) return [];
  const portReturns = [];
  for (let i = 0; i < n; i++) {
    let r = 0;
    for (let j = 0; j < assetReturns.length; j++) {
      r += (weights[j] || 0) * assetReturns[j][i];
    }
    portReturns.push(r);
  }
  return portReturns;
}

// Portfolio-level risk metrics from aligned daily return arrays + weights.
export function computePortfolioRisk(assetReturns, weights, benchReturns) {
  const portReturns = portfolioDailyReturns(assetReturns, weights);
  if (portReturns.length === 0) return null;

  const dailyVol = std(portReturns);
  const annualVol = dailyVol * Math.sqrt(252);
  const annualReturn = mean(portReturns) * 252;
  const sharpe = annualVol > 0 ? annualReturn / annualVol : 0;

  // Historical VaR (95%, 1-day): 5th percentile of daily returns
  const sorted = [...portReturns].sort((a, b) => a - b);
  const var95Idx = Math.max(0, Math.floor(sorted.length * 0.05) - 1);
  const histVar95 = sorted[var95Idx];

  // Parametric VaR (95%, 1-day): -1.645 * sigma (assumes normal distribution)
  const paramVar95 = -1.645 * dailyVol;

  // Max drawdown from equity curve
  let equity = 1, peak = 1, maxDD = 0;
  for (const r of portReturns) {
    equity *= (1 + r);
    if (equity > peak) peak = equity;
    const dd = (equity - peak) / peak;
    if (dd < maxDD) maxDD = dd;
  }

  // Portfolio beta: weighted average of individual betas
  let portBeta = 0;
  for (let j = 0; j < assetReturns.length; j++) {
    portBeta += (weights[j] || 0) * beta(assetReturns[j], benchReturns);
  }

  return {
    annualVol,
    annualReturn,
    sharpe,
    histVar95,
    paramVar95,
    maxDrawdown: maxDD,
    beta: portBeta,
  };
}

// Pairwise correlation matrix.
export function computeCorrelationMatrix(assetReturns, symbols) {
  const n = symbols.length;
  const matrix = [];
  for (let i = 0; i < n; i++) {
    const row = [];
    for (let j = 0; j < n; j++) {
      if (i === j) row.push(1);
      else row.push(Math.round(correlation(assetReturns[i], assetReturns[j]) * 100) / 100);
    }
    matrix.push(row);
  }
  return { symbols, matrix };
}

// Benchmark comparison: portfolio vs benchmark equity curves and performance metrics.
export function computeBenchmarkComparison(portReturns, benchReturns) {
  if (!portReturns.length || portReturns.length !== benchReturns.length) return null;

  // Cumulative equity curves rebased to 100
  const portEquity = [100 * (1 + portReturns[0])];
  const benchEquity = [100 * (1 + benchReturns[0])];
  for (let i = 1; i < portReturns.length; i++) {
    portEquity.push(portEquity[i - 1] * (1 + portReturns[i]));
    benchEquity.push(benchEquity[i - 1] * (1 + benchReturns[i]));
  }

  const portReturn = portEquity[portEquity.length - 1] / 100 - 1;
  const benchReturn = benchEquity[benchEquity.length - 1] / 100 - 1;
  const alpha = portReturn - benchReturn;

  // Tracking error: annualized std of excess returns
  const excessReturns = portReturns.map((r, i) => r - benchReturns[i]);
  const trackingError = std(excessReturns) * Math.sqrt(252);

  // Information ratio: annualized mean excess return / tracking error
  const informationRatio = trackingError > 0 ? (mean(excessReturns) * 252) / trackingError : 0;

  return {
    portReturn,
    benchReturn,
    alpha,
    trackingError,
    informationRatio,
    portEquity,
    benchEquity,
  };
}

// --- Matrix operations for portfolio optimization (Markowitz) ---

// Gaussian elimination with partial pivoting. Returns null for singular matrices.
export function matrixInverse(M) {
  const n = M.length;
  if (n === 0) return null;
  const aug = M.map((row, i) => {
    const identity = Array(n).fill(0);
    identity[i] = 1;
    return [...row, ...identity];
  });
  for (let i = 0; i < n; i++) {
    let maxRow = i;
    for (let k = i + 1; k < n; k++) {
      if (Math.abs(aug[k][i]) > Math.abs(aug[maxRow][i])) maxRow = k;
    }
    [aug[i], aug[maxRow]] = [aug[maxRow], aug[i]];
    if (Math.abs(aug[i][i]) < 1e-12) return null;
    for (let k = i + 1; k < n; k++) {
      const factor = aug[k][i] / aug[i][i];
      for (let j = i; j < 2 * n; j++) aug[k][j] -= factor * aug[i][j];
    }
  }
  for (let i = n - 1; i >= 0; i--) {
    const pivot = aug[i][i];
    for (let j = i; j < 2 * n; j++) aug[i][j] /= pivot;
    for (let k = i - 1; k >= 0; k--) {
      const factor = aug[k][i];
      for (let j = i; j < 2 * n; j++) aug[k][j] -= factor * aug[i][j];
    }
  }
  return aug.map((row) => row.slice(n));
}

export function matVec(M, v) {
  return M.map((row) => row.reduce((sum, m, i) => sum + m * v[i], 0));
}

// Sample covariance matrix from aligned daily return arrays.
export function covarianceMatrix(assetReturns) {
  const n = assetReturns.length;
  const t = assetReturns[0]?.length || 0;
  if (t < 2) return null;
  const means = assetReturns.map((r) => mean(r));
  const cov = Array(n).fill(0).map(() => Array(n).fill(0));
  for (let i = 0; i < n; i++) {
    for (let j = i; j < n; j++) {
      let sum = 0;
      for (let k = 0; k < t; k++) {
        sum += (assetReturns[i][k] - means[i]) * (assetReturns[j][k] - means[j]);
      }
      cov[i][j] = sum / (t - 1);
      cov[j][i] = cov[i][j];
    }
  }
  return cov;
}

export function portfolioVariance(weights, covMatrix) {
  let variance = 0;
  for (let i = 0; i < weights.length; i++) {
    for (let j = 0; j < weights.length; j++) {
      variance += weights[i] * weights[j] * covMatrix[i][j];
    }
  }
  return Math.max(0, variance);
}

export function portfolioReturn(weights, mu) {
  return weights.reduce((s, w, i) => s + w * mu[i], 0);
}

// Max Sharpe (tangency) portfolio, risk-free rate = 0.
// Unconstrained: w = Sigma^{-1} * mu, normalized to sum to 1.
export function maxSharpeWeights(mu, covMatrix) {
  const invCov = matrixInverse(covMatrix);
  if (!invCov) return null;
  const w = matVec(invCov, mu);
  const sum = w.reduce((a, b) => a + b, 0);
  if (Math.abs(sum) < 1e-10) return null;
  return w.map((x) => x / sum);
}

// Long-only approximation: clip negative weights and renormalize iteratively.
export function maxSharpeWeightsLongOnly(mu, covMatrix) {
  let w = maxSharpeWeights(mu, covMatrix);
  if (!w) return null;
  for (let iter = 0; iter < 10; iter++) {
    if (!w.some((x) => x < -1e-6)) break;
    w = w.map((x) => Math.max(0, x));
    const sum = w.reduce((a, b) => a + b, 0);
    if (sum < 1e-10) return null;
    w = w.map((x) => x / sum);
  }
  return w;
}

// Markowitz efficient frontier (unconstrained, risk-free rate = 0).
export function efficientFrontier(mu, covMatrix, nPoints = 40) {
  const invCov = matrixInverse(covMatrix);
  if (!invCov) return null;
  const n = mu.length;
  const ones = Array(n).fill(1);
  const invCovMu = matVec(invCov, mu);
  const invCovOnes = matVec(invCov, ones);
  const A = ones.reduce((s, _, i) => s + invCovMu[i], 0);
  const B = mu.reduce((s, m, i) => s + m * invCovMu[i], 0);
  const C = ones.reduce((s, _, i) => s + invCovOnes[i], 0);
  const D = B * C - A * A;
  if (Math.abs(D) < 1e-10) return null;

  const minReturn = A / C;
  const maxReturn = Math.max(...mu) * 1.2;
  const frontier = [];
  for (let i = 0; i < nPoints; i++) {
    const r = minReturn + (maxReturn - minReturn) * (i / (nPoints - 1));
    const variance = (C * r * r - 2 * A * r + B) / D;
    frontier.push({
      return: r,
      volatility: Math.sqrt(Math.max(0, variance)),
    });
  }
  return frontier;
}