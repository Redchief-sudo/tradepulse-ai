"""Deterministic market-regime classifier -- computed from a benchmark's
(e.g. SPY, BTC/USD) daily close-price series, NOT LLM estimation.

Phase 1 (this module): classifier correctness and calibration ONLY. There is
NO production caller -- nothing in scanner/coordinator.py, risk/engine.py,
or execution/gateway.py imports this module. Wiring it into a trading
decision is a separate, later, explicitly-approved phase.

History: originally ported from base44/shared/regime.ts, which computed a
SINGLE broad-market regime from a benchmark (SPY) and applied it to every
new buy that scan cycle -- never a per-candidate/per-instrument classifier.
That original assumed 5-minute observations throughout: a 288-bar window
("~1 day of 5-min bars"), `sqrt(252 * 78)` volatility annualization (78
5-min bars per 6.5-hour NYSE session), and RSI/SMA windows sized in 5-minute
bar-counts. Production has never had a 5-minute (or any intraday) bar
source -- the only bar-fetching call site in this codebase
(broker/alpaca_client.py::get_bars) has always requested "1Day" bars.
Feeding daily closes into the inherited formula wouldn't just be
dimensionally wrong on the annualization constant -- EVERY threshold
(volatility bands, trend-confirmation threshold, RSI window) was informally
tuned against whatever numeric range that 5-minute formula happens to
produce, not validated against daily behavior at all.

This version:
  - Takes an explicit `timeframe` -- only "1day" is supported (the only
    bar source this codebase has); anything else raises ValueError rather
    than silently being misinterpreted as some other interval.
  - Takes an explicit `calendar` ("equity" or "crypto") selecting an
    INDEPENDENTLY calibrated threshold set per asset class -- equity's
    252-trading-day/year calendar and volatility character are not shared
    with crypto's 365-day continuous calendar and structurally higher
    baseline volatility (verified live against real SPY and BTC/USD daily
    history -- see docs/ for the calibration report). Do not add a third
    calendar without equivalent calibration evidence.
  - Uses a 60-bar (not 288-bar) window and a 20-bar minimum history --
    daily-appropriate, not carried over from the 5-minute design.

Non-finite, non-positive, or insufficient closes fail closed to the same
"transition" fallback insufficient history already used -- a single bad
data point must never crash a caller or silently propagate NaN/garbage
into a regime label. An unsupported timeframe/calendar is a caller
contract error, not a data-quality issue, and raises instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Regime = Literal["low_vol_bull", "high_vol_bear", "range_bound_choppy", "liquidity_crisis", "transition"]
Timeframe = Literal["1day"]
Calendar = Literal["equity", "crypto"]

_STRATEGY_NOTES: dict[Regime, str] = {
    "low_vol_bull": "Trend-following - full position sizing, favor momentum breakouts",
    "high_vol_bear": "Defensive / capital preservation - reduce sizing, raise cash, tighten stops",
    "range_bound_choppy": "Mean-reversion - fade extremes, smaller sizes, tight stops",
    "liquidity_crisis": "De-risk / capital preservation - block new buys, exit weak positions",
    "transition": "Adaptive / cautious - moderate sizing, wait for confirmation",
}

# Fraction of the strategy's max position size allowed in each regime --
# carried over UNCHANGED from the original design in this phase. These are
# not re-derived here: Phase 1 calibrates classification correctness
# (does "high_vol_bear" mean what it says against real daily data), not
# sizing/P&L performance (whether 0.5 is the "right" bear-market multiplier)
# -- that requires backtesting against actual trade outcomes, out of scope
# until a production caller exists to backtest against. Monotonically
# sensible on their face (crisis lowest at 0.0, low_vol_bull highest at
# 1.0) and never exceed 1.0.
_POSITION_MULTIPLIERS: dict[Regime, Decimal] = {
    "low_vol_bull": Decimal("1.0"),
    "high_vol_bear": Decimal("0.5"),
    "range_bound_choppy": Decimal("0.7"),
    "liquidity_crisis": Decimal("0.0"),
    "transition": Decimal("0.75"),
}

# Bar-count windows -- consistent with how every other indicator in this
# codebase already works (strategy/indicators.py's RSI/SMA/etc. are all
# bar-count, not wall-clock, windows -- see compute_real_factors). Held
# UNIFORM across calendars (only the threshold VALUES below differ by
# calendar) -- calibration against real SPY and BTC/USD daily history
# found no evidence a different window length is needed per asset class.
WINDOW_BARS = 60
RSI_PERIOD = 14
SMA_SHORT_BARS = 12
# Below this many closes there isn't enough data to compute a stable
# 14-period RSI AND a meaningful trend regression -- fall back to
# "transition" rather than produce a classification on too little signal.
MIN_HISTORY_BARS = 20


@dataclass(frozen=True, slots=True)
class _CalendarThresholds:
    periods_per_year: int
    trend_threshold: Decimal  # total fractional drift over the WINDOW (not per-bar) beyond which a trend is "confirmed"
    high_vol_threshold: Decimal  # annualized realized-vol level regarded as "elevated" for this calendar
    crisis_vol_threshold: Decimal  # annualized realized-vol level regarded as "crisis" for this calendar


# Verified live against real SPY (equity) and BTC/USD (crypto) daily
# history spanning bull/bear/crisis/range/transition periods -- see the
# calibration report in docs/. Crypto's thresholds are NOT equity's
# thresholds reused: crypto's baseline annualized volatility (observed
# ~0.6-0.75 across bull, bear, AND ordinary conditions in the calibration
# sample) sits well above what would read as "crisis" for equities --
# reusing equity's 0.18/0.50 for crypto would misclassify nearly all
# normal crypto activity as high_vol_bear or worse.
_CALENDAR_THRESHOLDS: dict[Calendar, _CalendarThresholds] = {
    "equity": _CalendarThresholds(
        periods_per_year=252, trend_threshold=Decimal("0.08"),
        high_vol_threshold=Decimal("0.18"), crisis_vol_threshold=Decimal("0.50"),
    ),
    "crypto": _CalendarThresholds(
        periods_per_year=365, trend_threshold=Decimal("0.13"),
        high_vol_threshold=Decimal("0.80"), crisis_vol_threshold=Decimal("1.00"),
    ),
}


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    regime: Regime
    confidence: int
    strategy_note: str
    position_multiplier: Decimal
    realized_vol: Decimal | None
    rsi: Decimal | None
    trend: Decimal | None
    # Provenance -- what this classification actually used, not just what
    # the caller intended, so a persisted record is self-describing without
    # needing to separately track the call-site arguments.
    timeframe: Timeframe
    calendar: Calendar
    observation_bars: int


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _transition(timeframe: Timeframe, calendar: Calendar, observation_bars: int) -> RegimeClassification:
    return RegimeClassification(
        regime="transition",
        confidence=30,
        strategy_note=_STRATEGY_NOTES["transition"],
        position_multiplier=_POSITION_MULTIPLIERS["transition"],
        realized_vol=None,
        rsi=None,
        trend=None,
        timeframe=timeframe,
        calendar=calendar,
        observation_bars=observation_bars,
    )


def classify_regime(
    closes: list[Decimal], *, timeframe: Timeframe = "1day", calendar: Calendar = "equity",
) -> RegimeClassification:
    """closes must be oldest-first. Raises ValueError for an unsupported
    timeframe/calendar (a caller contract error) -- never silently
    interprets daily observations as some other interval. Falls back to
    "transition" (never raises) for insufficient, non-positive, or
    non-finite price data -- a data-quality condition a live scan cycle
    must be able to continue past, not crash on."""
    if timeframe not in ("1day",):
        raise ValueError(f"classify_regime only supports timeframe='1day' (the only bar source this codebase has) -- got {timeframe!r}")
    if calendar not in _CALENDAR_THRESHOLDS:
        raise ValueError(f"unsupported calendar {calendar!r} -- expected one of {sorted(_CALENDAR_THRESHOLDS)}")
    thresholds = _CALENDAR_THRESHOLDS[calendar]

    if not closes or len(closes) < MIN_HISTORY_BARS or any((not c.is_finite()) or c <= 0 for c in closes):
        return _transition(timeframe, calendar, len(closes))

    values = [float(c) for c in closes]
    series = values[-min(len(values), WINDOW_BARS):]
    returns = [(series[i] - series[i - 1]) / series[i - 1] for i in range(1, len(series)) if series[i - 1] > 0]
    realized_vol = _std(returns) * math.sqrt(thresholds.periods_per_year)
    rsi_val = _rsi(series, min(RSI_PERIOD, len(series) - 1))

    n = len(series)
    mean_y = sum(series) / n
    num = 0.0
    den = 0.0
    for i in range(n):
        x = i - (n - 1) / 2
        num += x * (series[i] - mean_y)
        den += x**2
    slope = num / den if den > 0 else 0.0
    trend = (slope / mean_y) * n if mean_y > 0 else 0.0
    last = series[-1]
    sma_short = sum(series[-SMA_SHORT_BARS:]) / min(SMA_SHORT_BARS, len(series))

    trend_thresh = float(thresholds.trend_threshold)
    high_vol = realized_vol > float(thresholds.high_vol_threshold)
    crisis_vol = realized_vol > float(thresholds.crisis_vol_threshold)
    # Symmetric RSI gate (50/50) on both sides -- the original 5-minute
    # design used an asymmetric 50/45 split with no documented rationale;
    # calibration against real daily SPY/BTC data found no evidence for
    # that asymmetry (every real bearish/crisis calibration sample cleared
    # RSI<45 anyway, except one real BTC bear sample at RSI 46.9 that a
    # symmetric <50 correctly catches and an unexplained <45 would miss).
    bullish = trend > trend_thresh and last >= sma_short and rsi_val > 50
    bearish = trend < -trend_thresh and last <= sma_short and rsi_val < 50

    regime: Regime = "transition"
    if crisis_vol and bearish:
        regime = "liquidity_crisis"
    elif high_vol and bearish:
        regime = "high_vol_bear"
    elif not high_vol and bullish:
        regime = "low_vol_bull"
    elif abs(trend) <= trend_thresh and not high_vol:
        regime = "range_bound_choppy"

    agree = 0
    if (trend > 0) == bullish:
        agree += 1
    if (last >= mean_y) == bullish:
        agree += 1
    if (rsi_val > 50) == bullish:
        agree += 1
    if (rsi_val < 50) == bearish:
        agree += 1
    confidence = min(95, max(30, round(40 + (agree / 4) * 50 + abs(trend) * 200)))

    return RegimeClassification(
        regime=regime,
        confidence=confidence,
        strategy_note=_STRATEGY_NOTES[regime],
        position_multiplier=_POSITION_MULTIPLIERS[regime],
        realized_vol=Decimal(str(round(realized_vol, 3))),
        rsi=Decimal(str(round(rsi_val, 1))),
        trend=Decimal(str(round(trend, 4))),
        timeframe=timeframe,
        calendar=calendar,
        observation_bars=n,
    )
