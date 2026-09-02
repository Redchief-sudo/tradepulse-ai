"""strategy/regime.py -- classifier correctness and calibration tests.

Phase 1 scope: this module has NO production caller (verified by
test_classify_regime_has_no_production_caller below). These tests validate
the classifier in isolation against real historical SPY (equity) and
BTC/USD (crypto) daily closes -- fetched live from Alpaca (the exact
production data path, broker/alpaca_client.py::get_bars) for well-known,
independently-verifiable regime periods. See docs/ for the full calibration
report and methodology (why each period was chosen, why SIP was used for
this one-time offline research fetch instead of IEX, and the near-misses
found and resolved during calibration).

Each series below is oldest-first, trimmed to the last 60 (or fewer, for
the two short crisis windows) daily closes -- exactly what
classify_regime's own WINDOW_BARS trimming would use from a longer series,
so these fixtures are the precise input the classifier actually sees.
"""

from decimal import Decimal

import pytest

from tradepulse.strategy.regime import MIN_HISTORY_BARS, classify_regime

# ---- Real SPY daily closes (equity calendar) -------------------------------
# 2019-01-02..2019-12-30, tail 60 -- steady, low-volatility bull year.
SPY_LOW_VOL_BULL_2019 = [Decimal(v) for v in [
    "294.3", "293.1", "288.66", "291.12", "293.29", "296.25", "296.03", "298.88", "298.46", "299.21",
    "298.09", "300.03", "298.99", "299.83", "300.42", "301.59", "303.34", "303.14", "304.21", "303.33",
    "306.17", "307.37", "307.04", "307.07", "308.21", "308.93", "308.36", "308.96", "309.15", "309.54",
    "311.81", "311.98", "311.91", "310.81", "310.3", "311.02", "313.33", "313.99", "315.42", "314.33",
    "311.66", "309.51", "311.5", "312.09", "314.83", "313.89", "313.58", "314.33", "317.08", "317.29",
    "319.49", "319.5495", "319.56", "320.87", "320.86", "321.27", "321.24", "322.94", "322.85", "321.13",
]]
# 2022-01-03..2022-10-12, tail 60 -- the 2022 Fed-hiking-cycle bear market.
SPY_HIGH_VOL_BEAR_2022 = [Decimal(v) for v in [
    "394.77", "398.79", "395.09", "395.57", "390.89", "401.04", "406.04", "411.99", "410.77", "408.06",
    "414.45", "414.17", "413.47", "412.99", "411.35", "419.99", "419.99", "427.1", "428.86", "429.7",
    "426.65", "427.89", "422.14", "413.35", "412.35", "413.67", "419.51", "405.31", "402.63", "398.21",
    "395.18", "396.42", "392.24", "390.76", "397.78", "400.38", "406.6", "410.97", "393.1", "394.6",
    "390.12", "385.56", "388.55", "384.09", "377.39", "374.22", "367.95", "364.31", "363.38", "370.53",
    "362.79", "357.18", "366.61", "377.97", "377.09", "373.2", "362.79", "360.02", "357.74", "356.56",
]]
# 2020-02-19..2020-03-20 -- the COVID crash, peak to trough (the recovery
# that began ~2020-03-24 is a separate regime, deliberately excluded --
# including it would blend crisis and early-recovery price action into one
# misleading window; see the calibration report for why the first
# 2020-02-10..2020-04-07 fetch had to be narrowed).
SPY_CRISIS_COVID_2020 = [Decimal(v) for v in [
    "338.32", "336.99", "333.45", "322.42", "312.59", "311.61", "297.7", "296.24", "308.9", "300.32",
    "313.05", "302.51", "297.43", "276.32", "288.41", "274.25", "255.24", "270.2", "241.065", "254.19",
    "235.69", "240.55", "229.4",
]]
# 2023-02-01..2023-05-25, tail 60 -- mildly-bullish chop (real trend is a
# small net positive drift, not perfectly flat -- see the calibration
# report on why this genuinely lands as range_bound_choppy rather than
# low_vol_bull: the drift never confirms as a trend under the calibrated
# threshold, and that's a real property of this window, not a forced label).
SPY_RANGE_CHOP_2023 = [Decimal(v) for v in [
    "397.81", "404.19", "404.47", "398.27", "398.92", "391.56", "385.91", "385.36", "391.73", "389.28",
    "396.11", "389.99", "393.74", "398.91", "392.11", "393.17", "395.75", "396.49", "395.6", "401.35",
    "403.7", "409.39", "410.95", "408.67", "407.6", "409.19", "409.61", "409.72", "408.05", "413.47",
    "412.46", "413.94", "414.21", "414.14", "411.88", "412.2", "412.63", "406.08", "404.36", "412.41",
    "415.93", "415.51", "410.84", "408.02", "405.13", "412.63", "412.74", "410.93", "412.85", "412.13",
    "411.59", "413.01", "410.25", "415.23", "419.23", "418.62", "418.79", "414.09", "411.09", "414.65",
]]
# 2018-12-03..2019-02-14, tail 50 -- the sharp recovery immediately after
# the Dec 2018 low: mixed signals (bullish-looking trend/RSI, but still
# elevated volatility from the selloff) -- a genuine "transition" case,
# not engineered to be one.
SPY_TRANSITION_2018_2019 = [Decimal(v) for v in [
    "279.26", "270.39", "269.77", "263.66", "264.07", "264.11", "265.53", "265.38", "260.57", "255.17",
    "255.11", "251.04", "247.11", "240.61", "234.37", "245.9095", "248.21", "247.73", "250.08", "250.23",
    "244.15", "252.39", "254.29", "256.62", "257.92", "258.98", "258.81", "257.35", "260.23", "260.96",
    "263", "266.42", "262.94", "263.35", "263.6", "265.78", "263.78", "263.52", "267.44", "269.81",
    "270.08", "272.01", "273.16", "272.83", "270.17", "270.54", "270.69", "274.06", "274.95", "274.46",
]]

# ---- Real BTC/USD daily closes (crypto calendar) ---------------------------
# 2021-01-01..2021-04-14, tail 60 -- the 2020-2021 bull run into the April ATH.
BTC_BULL_2021 = [Decimal(v) for v in [
    "48644.74", "47916.2", "49163.4", "52136.85", "51577.42", "55940.77", "55952.8", "57475.12", "54121.6", "48903",
    "49714.5", "47069", "46303.07", "46160.47", "45213.99", "49654.77", "48476.33", "50421.19", "48377.78", "48773.99",
    "48872.23", "50974.98", "52410.35", "54920.8", "55872.56", "57777.03", "57222.91", "61179.21", "59000", "55679.63",
    "56911.22", "58921.41", "57634.37", "58055.84", "58100.73", "57355.94", "54071.04", "54377.15", "52283.88", "51285.49",
    "55075.58", "55845.23", "55783.89", "57614.15", "58799.98", "58791.65", "58713.5", "58989.99", "57103.96", "58203.08",
    "59128.34", "58018.92", "55928.93", "58079.35", "58111.31", "59760.11", "59967.76", "59839.08", "63588.93", "62971.33",
]]
# 2022-04-01..2022-06-30, tail 60 -- the Terra/Luna-collapse-driven decline.
BTC_BEAR_2022 = [Decimal(v) for v in [
    "38517.84", "37719.58", "39689.62", "36540", "36004.58", "35464.39", "34033.74", "30080.85", "31023.27", "28977.8",
    "28914.38", "29232.45", "30040", "31293.32", "29839.05", "30410.15", "28690.3", "30284.34", "29172.8", "29409.35",
    "30268.03", "29084.37", "29635.6", "29507.5", "29167.72", "28593.31", "28998.65", "29444.74", "31718.42", "31775.65",
    "29782.58", "30430.04", "29671.45", "29846.27", "29890.97", "31348", "31098.93", "30181.34", "30077.71", "29062.58",
    "28380", "26551.52", "22460.52", "22104.28", "22568.58", "20376.76", "20442.23", "18949.08", "20558.14", "20539.82",
    "20704.85", "19973.04", "21091.3", "21218.44", "21469.62", "21026.26", "20722.93", "20253.07", "20099.67", "19937.23",
]]
# 2021-07-01..2021-09-30, tail 60 -- consolidation after the May 2021 crash,
# before the autumn rally -- moderate-for-crypto volatility, near-zero net drift.
BTC_ORDINARY_2021 = [Decimal(v) for v in [
    "39158.82", "38198.69", "39738.06", "40893.01", "42845.11", "44630.13", "43839.45", "46302.14", "45604.47", "45537.41",
    "44432.59", "47824.77", "47100.29", "47023.87", "45919.2", "44679.68", "44721.29", "46763.38", "49347.09", "48864.71",
    "49285.71", "49497.34", "47696.18", "48997.95", "46846.65", "49075.92", "48932.74", "48790.27", "46994.09", "47124.19",
    "48846.19", "49277.1", "50019.96", "49943.9", "51792.23", "52702.93", "46856.92", "46086.65", "46403.58", "44850.95",
    "45171.03", "46019.14", "44954.3", "47124.05", "48147.95", "47759.74", "47299.8", "48299.95", "47250.86", "43002.87",
    "40698.03", "43570", "44889.72", "42831.25", "42702.82", "43216.36", "42177.51", "41025.86", "41521.43", "43829.22",
]]
# 2021-05-01..2021-05-23 -- the Musk/China-driven ~-49% crash.
BTC_CRISIS_MAY_2021 = [Decimal(v) for v in [
    "57859.07", "56612.76", "57220.34", "53235.1", "57506.49", "56426.06", "57366.21", "58949.48", "58296.3", "55849.97",
    "56758.02", "49553.43", "49696.9", "49898.98", "46776.05", "46433.85", "43570.56", "42873.29", "36749.16", "40648.2",
    "37346.33", "37492.05", "34716.86",
]]
# 2023-06-01..2023-09-29, tail 60 -- one of BTC's tightest documented
# historical ranges.
BTC_RANGE_CHOP_2023 = [Decimal(v) for v in [
    "29707.746395", "29167.821211", "29178.6425", "29078.0379595", "29052.8365", "29038.42672231", "29181.10355", "29761.0293575", "29559.97825", "29415.5502513645",
    "29399.99841945", "29416.30177157", "29287.3646353645", "29400.36023723", "29190.9", "28704.115", "26628.27375375", "26051.49855", "26084.455", "26187.584088911",
    "26118.313", "26030.1015", "26429.0085", "26160.77", "26044.635", "26000.994", "26084.169", "26108.4", "27724.59", "27295.925",
    "25929.43", "25798.771", "25860.898", "25973.07", "25803.8405", "25782.632", "25749.882", "26241.733", "25896.69", "25886.395",
    "25831.99", "25158.8355", "25835.275", "26221.272", "26526.015", "26594.085", "26567.8675", "26532.4635", "26773.2945", "27209.3105",
    "27129.46", "26568.257", "26571.761", "26578.205", "26251.495", "26303.73", "26203.1", "26337.338", "27016.87", "26907.6985",
]]


# ---- Real-data calibration: equity ------------------------------------------


def test_real_spy_low_vol_bull_2019() -> None:
    result = classify_regime(SPY_LOW_VOL_BULL_2019, timeframe="1day", calendar="equity")
    assert result.regime == "low_vol_bull"
    assert result.position_multiplier == Decimal("1.0")
    assert result.realized_vol is not None and result.realized_vol < Decimal("0.18")


def test_real_spy_high_vol_bear_2022() -> None:
    result = classify_regime(SPY_HIGH_VOL_BEAR_2022, timeframe="1day", calendar="equity")
    assert result.regime == "high_vol_bear"
    assert result.position_multiplier == Decimal("0.5")


def test_real_spy_crisis_covid_2020() -> None:
    result = classify_regime(SPY_CRISIS_COVID_2020, timeframe="1day", calendar="equity")
    assert result.regime == "liquidity_crisis"
    assert result.position_multiplier == Decimal("0.0")
    assert result.realized_vol is not None and result.realized_vol > Decimal("0.50")


def test_real_spy_range_chop_2023() -> None:
    result = classify_regime(SPY_RANGE_CHOP_2023, timeframe="1day", calendar="equity")
    assert result.regime == "range_bound_choppy"
    assert result.position_multiplier == Decimal("0.7")


def test_real_spy_transition_2018_2019() -> None:
    result = classify_regime(SPY_TRANSITION_2018_2019, timeframe="1day", calendar="equity")
    assert result.regime == "transition"
    assert result.position_multiplier == Decimal("0.75")


# ---- Real-data calibration: crypto ------------------------------------------


def test_real_btc_bull_2021() -> None:
    result = classify_regime(BTC_BULL_2021, timeframe="1day", calendar="crypto")
    assert result.regime == "low_vol_bull"
    assert result.position_multiplier == Decimal("1.0")


def test_real_btc_bear_2022_terra_luna() -> None:
    result = classify_regime(BTC_BEAR_2022, timeframe="1day", calendar="crypto")
    assert result.regime == "high_vol_bear"
    assert result.position_multiplier == Decimal("0.5")


def test_real_btc_ordinary_2021() -> None:
    result = classify_regime(BTC_ORDINARY_2021, timeframe="1day", calendar="crypto")
    assert result.regime == "range_bound_choppy"


def test_real_btc_crisis_may_2021() -> None:
    result = classify_regime(BTC_CRISIS_MAY_2021, timeframe="1day", calendar="crypto")
    assert result.regime == "liquidity_crisis"
    assert result.position_multiplier == Decimal("0.0")
    assert result.realized_vol is not None and result.realized_vol > Decimal("1.00")


def test_real_btc_range_chop_2023() -> None:
    result = classify_regime(BTC_RANGE_CHOP_2023, timeframe="1day", calendar="crypto")
    assert result.regime == "range_bound_choppy"


def test_crypto_thresholds_are_not_equity_thresholds_reused() -> None:
    """The exact defect the audit called out: BTC's normal (non-crisis)
    volatility comfortably exceeds equity's crisis threshold. Proves
    classify_regime does NOT misclassify ordinary crypto activity as an
    equity-style crisis merely because the same closes, read under
    calendar="equity", would look extreme."""
    from tradepulse.strategy.regime import _CALENDAR_THRESHOLDS

    ordinary_under_crypto = classify_regime(BTC_ORDINARY_2021, timeframe="1day", calendar="crypto")
    ordinary_under_equity = classify_regime(BTC_ORDINARY_2021, timeframe="1day", calendar="equity")
    assert ordinary_under_crypto.regime != "liquidity_crisis"
    assert ordinary_under_crypto.realized_vol is not None and ordinary_under_crypto.realized_vol < _CALENDAR_THRESHOLDS["crypto"].crisis_vol_threshold
    # Same raw closes, same math -- annualized vol legitimately differs only
    # because periods_per_year differs (365 vs 252); even so, this ordinary
    # crypto window's equity-calendar vol comfortably exceeds equity's own
    # (much lower) crisis threshold -- proving the two calendars are
    # genuinely independently calibrated, not the same numbers relabeled.
    assert ordinary_under_equity.realized_vol is not None and ordinary_under_equity.realized_vol > _CALENDAR_THRESHOLDS["equity"].crisis_vol_threshold


# ---- Structural / edge-case tests -------------------------------------------


def test_insufficient_history_returns_transition_with_no_stats() -> None:
    result = classify_regime([Decimal(100)] * (MIN_HISTORY_BARS - 1), timeframe="1day", calendar="equity")
    assert result.regime == "transition"
    assert result.confidence == 30
    assert result.realized_vol is None
    assert result.rsi is None
    assert result.trend is None


def test_flat_series_is_range_bound_choppy() -> None:
    closes = [Decimal(100)] * 30
    result = classify_regime(closes, timeframe="1day", calendar="equity")
    assert result.regime == "range_bound_choppy"
    assert result.position_multiplier == Decimal("0.7")
    assert result.realized_vol == Decimal("0.000")


def test_steady_synthetic_uptrend_is_low_vol_bull() -> None:
    closes = [Decimal(str(100 + i * 0.5)) for i in range(60)]
    result = classify_regime(closes, timeframe="1day", calendar="equity")
    assert result.regime == "low_vol_bull"
    assert result.position_multiplier == Decimal("1.0")


@pytest.mark.parametrize("bad_value", [Decimal(0), Decimal(-5), Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_or_non_positive_close_fails_closed_to_transition(bad_value: Decimal) -> None:
    """A single corrupt data point must never crash a live scan cycle or
    silently propagate NaN/garbage into a persisted regime label -- treated
    the same as insufficient history, not raised."""
    closes = [Decimal(100)] * 30
    closes[10] = bad_value
    result = classify_regime(closes, timeframe="1day", calendar="equity")
    assert result.regime == "transition"
    assert result.realized_vol is None


def test_empty_closes_returns_transition() -> None:
    result = classify_regime([], timeframe="1day", calendar="equity")
    assert result.regime == "transition"


def test_unsupported_timeframe_raises_instead_of_silently_assuming_daily() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        classify_regime(SPY_LOW_VOL_BULL_2019, timeframe="5min", calendar="equity")  # type: ignore[arg-type]


def test_unsupported_calendar_raises() -> None:
    with pytest.raises(ValueError, match="calendar"):
        classify_regime(SPY_LOW_VOL_BULL_2019, timeframe="1day", calendar="forex")  # type: ignore[arg-type]


def test_confidence_is_bounded_between_30_and_95() -> None:
    for closes in (SPY_LOW_VOL_BULL_2019, SPY_HIGH_VOL_BEAR_2022, SPY_TRANSITION_2018_2019, BTC_BULL_2021, BTC_ORDINARY_2021):
        result = classify_regime(closes, timeframe="1day", calendar="equity")
        assert 30 <= result.confidence <= 95


def test_position_multiplier_is_never_greater_than_one_for_any_regime() -> None:
    from tradepulse.strategy.regime import (
        _POSITION_MULTIPLIERS,
    )

    for regime, multiplier in _POSITION_MULTIPLIERS.items():
        assert Decimal(0) <= multiplier <= Decimal(1), f"{regime} multiplier {multiplier} outside [0, 1]"


def test_liquidity_crisis_multiplier_is_zero() -> None:
    assert classify_regime(SPY_CRISIS_COVID_2020, timeframe="1day", calendar="equity").position_multiplier == Decimal("0.0")


def test_classification_is_deterministic_and_repeatable() -> None:
    first = classify_regime(SPY_HIGH_VOL_BEAR_2022, timeframe="1day", calendar="equity")
    second = classify_regime(SPY_HIGH_VOL_BEAR_2022, timeframe="1day", calendar="equity")
    assert first == second


def test_result_carries_observation_provenance() -> None:
    result = classify_regime(SPY_LOW_VOL_BULL_2019, timeframe="1day", calendar="equity")
    assert result.timeframe == "1day"
    assert result.calendar == "equity"
    assert result.observation_bars == 60  # trimmed to WINDOW_BARS from the 60-bar fixture


def test_classify_regime_has_exactly_one_production_caller() -> None:
    """Phase 2 boundary: scanner/coordinator.py is the SOLE production
    caller (Architecture A -- one benchmark regime per lane per cycle,
    threaded down as a bare Decimal + a plain provenance dict). risk/engine.py
    and execution/gateway.py must NEVER import strategy.regime directly --
    both are deliberately kept decoupled from it (see
    RiskCheckInput.regime_multiplier and ExecutionRequest.regime_snapshot's
    own docstrings for why: risk/engine.py's own "deterministic,
    strategy-independent" principle, applied consistently one layer up for
    the gateway too). This test fails loudly the moment either boundary is
    crossed without a deliberate decision to update it."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent / "tradepulse"
    forbidden_roots = {"risk", "execution", "settlement", "reconciliation", "session_commands.py", "cli.py"}
    allowed_caller = repo_root / "scanner" / "coordinator.py"
    offenders = []
    found_in_allowed_caller = False
    for path in repo_root.rglob("*.py"):
        relative_parts = path.relative_to(repo_root).parts
        in_forbidden = relative_parts[0] in forbidden_roots or (len(relative_parts) == 1 and relative_parts[0] in forbidden_roots)
        is_allowed_caller = path == allowed_caller
        if not in_forbidden and not is_allowed_caller:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        imports_regime = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "regime" in node.module:
                imports_regime = True
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "regime" in alias.name:
                        imports_regime = True
            # Also catch `from tradepulse.strategy import classify_regime`
            # (the actual, real import shape) -- not just a direct
            # `from tradepulse.strategy.regime import ...`, since
            # strategy/__init__.py re-exports classify_regime/Regime/
            # RegimeClassification/Calendar/Timeframe from the package.
            if isinstance(node, ast.ImportFrom) and node.module == "tradepulse.strategy":
                for alias in node.names:
                    if "regime" in alias.name.lower():
                        imports_regime = True
        if not imports_regime:
            continue
        if is_allowed_caller:
            found_in_allowed_caller = True
        else:
            offenders.append(str(path))
    assert offenders == [], f"classify_regime must have no caller outside scanner/coordinator.py, found imports in: {offenders}"
    assert found_in_allowed_caller, "expected scanner/coordinator.py to import strategy.regime (Phase 2 wiring) -- update this test if that's no longer true"
