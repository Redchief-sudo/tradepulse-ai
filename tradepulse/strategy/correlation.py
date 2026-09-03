"""Portfolio Optimization -- pairwise return correlation, used by
scanner/coordinator.py to demote (never hard-reject) a candidate whose price
behavior is highly correlated with something already approved this cycle or
already held.
"""

from __future__ import annotations


def pearson_correlation(closes_a: list[float], closes_b: list[float]) -> float | None:
    """Pearson correlation of DAILY RETURNS, not raw prices -- prices are
    non-stationary (shared trend/drift would overstate similarity between
    two assets that just happen to both be rising). None on fewer than 2
    overlapping returns or zero variance in either series, matching every
    other indicator's None-on-insufficient-data convention in
    strategy/indicators.py."""
    returns_a = [(closes_a[i] - closes_a[i - 1]) / closes_a[i - 1] for i in range(1, len(closes_a)) if closes_a[i - 1] != 0]
    returns_b = [(closes_b[i] - closes_b[i - 1]) / closes_b[i - 1] for i in range(1, len(closes_b)) if closes_b[i - 1] != 0]
    n = min(len(returns_a), len(returns_b))
    if n < 2:
        return None
    a, b = returns_a[-n:], returns_b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    covariance = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    variance_a = sum((x - mean_a) ** 2 for x in a)
    variance_b = sum((x - mean_b) ** 2 for x in b)
    if variance_a == 0 or variance_b == 0:
        return None
    return covariance / (variance_a**0.5 * variance_b**0.5)
