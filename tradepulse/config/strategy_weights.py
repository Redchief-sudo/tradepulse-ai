"""Default factor-weight vector for the scanner's deterministic composite
gate -- see strategy/composite.py.

These are the *relative* weights from the audited base44 source
(quantScore.ts's default `{technical: 25, fundamental: 25, sentiment: 20,
momentum: 15, risk: 15}`), with the fundamental/sentiment factors this MVP
never computes dropped. weighted_composite() already divides by the sum of
the weights actually passed in, so keeping the same relative proportions
among technical/momentum/risk is a faithful port, not an invented number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tradepulse.models import StrategyWeights


def default_strategy_weights(now: datetime) -> StrategyWeights:
    return StrategyWeights(
        version="v1", technical_weight=Decimal("25"), momentum_weight=Decimal("15"), risk_weight=Decimal("15"),
        effective_at=now,
    )


# Strategy Sophistication Phase 1 -- regime-conditioned weight profiles.
#
# NOT CURRENTLY WIRED IN. PLACEHOLDER -- NOT YET CALIBRATED against real
# backtested outcomes. Directionally reasoned (momentum-heavy in
# low_vol_bull, defensive/relative-strength-heavy in high_vol_bear,
# mean-reversion/technical-heavy in range_bound_choppy) but never validated
# against real trade results. scanner/coordinator.py originally called
# regime_conditioned_weights() below to condition the live composite gate
# on these profiles; reverted ahead of a 60-day prove-edge baseline
# (Rev.84 audit) -- candidate scoring/ranking/capital allocation must not
# depend on an unvalidated hypothesis, unlike regime.py's own sizing
# multiplier (risk/engine.py's regime_multiplier), which IS grounded in
# real historical market statistics (see
# docs/regime-classifier-phase1-calibration.md) and remains active. Kept
# here, tested, and callable for a future proper walk-forward calibration
# pass (compare against the fixed baseline on held-out data, isolate each
# factor's contribution) -- not deleted, just not trusted with live capital
# yet.
#
# Deliberately keyed by a BARE STRING regime label, not strategy.regime.Regime
# -- config/ must never import strategy.regime types here, mirroring the
# same decoupling already applied to RiskCheckInput.regime_multiplier (bare
# Decimal) and ExecutionRequest.regime_snapshot (bare dict). The caller
# (scanner/coordinator.py) is the only place allowed to know both "regime"
# and "weights" are related concepts.
_REGIME_WEIGHT_PROFILES: dict[str, tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]] = {
    # (technical, momentum, risk, liquidity, risk_quality, relative_strength)
    "low_vol_bull": (Decimal("20"), Decimal("30"), Decimal("10"), Decimal("15"), Decimal("10"), Decimal("15")),
    "high_vol_bear": (Decimal("20"), Decimal("10"), Decimal("15"), Decimal("10"), Decimal("20"), Decimal("25")),
    "range_bound_choppy": (Decimal("35"), Decimal("5"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15")),
    "transition": (Decimal("25"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15")),
    "unavailable": (Decimal("25"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15"), Decimal("15")),  # == transition
    # liquidity_crisis: candidates never actually reach weighting -- the
    # scanner hard-blocks new entries in this regime at the signal layer
    # (scanner/coordinator.py's LIQUIDITY_CRISIS_NEW_ENTRIES_SUPPRESSED
    # gate) before compute_real_factors/weighted_composite ever runs, and
    # risk/engine.py's regime_multiplier=0 is the independent sizing-layer
    # backstop. This row exists only as defense-in-depth for any future
    # non-execution caller (e.g. a "what would this have scored" dashboard)
    # that doesn't have those gates -- deliberately the most conservative
    # profile.
    "liquidity_crisis": (Decimal("15"), Decimal("5"), Decimal("15"), Decimal("10"), Decimal("25"), Decimal("30")),
}
_DEFAULT_WEIGHT_PROFILE = _REGIME_WEIGHT_PROFILES["transition"]  # any unrecognized/future label fails closed to this


def regime_conditioned_weights(base: StrategyWeights, regime_label: str, now: datetime) -> StrategyWeights:
    """Returns a NEW StrategyWeights carrying the given regime's relative
    factor proportions -- `base`'s own weight values are never blended in,
    fully replaced (matches classify_regime's own "pick the whole
    calibrated set for this label" pattern rather than a partial per-field
    override). `base` supplies only `source` provenance passthrough.
    `version` records which profile was actually used, so a persisted
    Opportunity/ScanRun row derived from the result is self-describing."""
    technical, momentum, risk, liquidity, risk_quality, relative_strength = _REGIME_WEIGHT_PROFILES.get(
        regime_label, _DEFAULT_WEIGHT_PROFILE
    )
    return StrategyWeights(
        version=f"{base.version}+regime:{regime_label}", technical_weight=technical, momentum_weight=momentum,
        risk_weight=risk, effective_at=now, source=base.source,
        liquidity_weight=liquidity, risk_quality_weight=risk_quality, relative_strength_weight=relative_strength,
    )
