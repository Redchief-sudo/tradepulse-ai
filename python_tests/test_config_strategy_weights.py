from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.config.strategy_weights import default_strategy_weights, regime_conditioned_weights

NOW = datetime(2026, 8, 15, tzinfo=UTC)

_KNOWN_REGIME_LABELS = (
    "low_vol_bull", "high_vol_bear", "range_bound_choppy", "transition", "unavailable", "liquidity_crisis",
)


def test_regime_conditioned_weights_selects_a_distinct_profile_per_known_label() -> None:
    base = default_strategy_weights(NOW)
    profiles = {label: regime_conditioned_weights(base, label, NOW) for label in _KNOWN_REGIME_LABELS}
    # Every profile sums to a positive total and produces a real StrategyWeights.
    for label, weights in profiles.items():
        total = (
            weights.technical_weight + weights.momentum_weight + weights.risk_weight
            + weights.liquidity_weight + weights.risk_quality_weight + weights.relative_strength_weight
        )
        assert total > 0, label
    # low_vol_bull is trend/momentum-heavy; high_vol_bear leans defensive
    # (risk_quality + relative_strength) rather than momentum-chasing.
    assert profiles["low_vol_bull"].momentum_weight > profiles["high_vol_bear"].momentum_weight
    assert profiles["high_vol_bear"].risk_quality_weight > profiles["low_vol_bull"].risk_quality_weight


def test_regime_conditioned_weights_unavailable_matches_transition_profile() -> None:
    base = default_strategy_weights(NOW)
    unavailable = regime_conditioned_weights(base, "unavailable", NOW)
    transition = regime_conditioned_weights(base, "transition", NOW)
    assert unavailable.technical_weight == transition.technical_weight
    assert unavailable.momentum_weight == transition.momentum_weight
    assert unavailable.risk_weight == transition.risk_weight
    assert unavailable.liquidity_weight == transition.liquidity_weight
    assert unavailable.risk_quality_weight == transition.risk_quality_weight
    assert unavailable.relative_strength_weight == transition.relative_strength_weight


def test_regime_conditioned_weights_falls_back_to_transition_profile_for_unrecognized_label() -> None:
    base = default_strategy_weights(NOW)
    unknown = regime_conditioned_weights(base, "some_future_regime_label", NOW)
    transition = regime_conditioned_weights(base, "transition", NOW)
    assert unknown.technical_weight == transition.technical_weight
    assert unknown.momentum_weight == transition.momentum_weight
    assert unknown.risk_weight == transition.risk_weight


def test_regime_conditioned_weights_version_records_which_profile_was_used() -> None:
    base = default_strategy_weights(NOW)
    weights = regime_conditioned_weights(base, "low_vol_bull", NOW)
    assert weights.version == f"{base.version}+regime:low_vol_bull"


def test_regime_conditioned_weights_liquidity_crisis_is_the_most_defensive_profile() -> None:
    base = default_strategy_weights(NOW)
    crisis = regime_conditioned_weights(base, "liquidity_crisis", NOW)
    low_vol_bull = regime_conditioned_weights(base, "low_vol_bull", NOW)
    assert crisis.risk_quality_weight > low_vol_bull.risk_quality_weight
    assert crisis.momentum_weight < low_vol_bull.momentum_weight


def test_regime_conditioned_weights_source_passthrough_from_base() -> None:
    base = default_strategy_weights(NOW)
    weights = regime_conditioned_weights(base, "transition", NOW)
    assert weights.source == base.source
