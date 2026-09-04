"""No-lookahead entry generation + exit simulation for the exit-parameter
calibration backtest.

Entries: reuses tradepulse.strategy.compute_real_factors/weighted_composite/
signal_from_composite and scanner/coordinator.py's own
_atr_stop_loss_price/_stop_loss_price DIRECTLY (all pure, synchronous,
side-effect-free) -- zero reimplementation of the entry-decision logic that
production actually runs today (fixed baseline weights, per the Rev.85
revert). No AI-recommendation gate is simulated: every BUY/STRONG_BUY
deterministic signal is treated as an independent hypothetical entry. See
docs/exit-parameter-calibration.md for why that's a real, permanent scope
limit (historical AI opinions can't be reconstructed) and why it's still
faithful to production's actual same-symbol re-entry behavior (verified
against source: no same-symbol block exists beyond in-flight-order dedup).

Exits: monitor/coordinator.py's _ratchet_stop is DB-coupled (mutates a
Holding through PersistenceRepositories) and can't be called directly from a
pure simulation -- _simulate_exit below is a field-for-field mirror of its
math (see the comment at its definition for the exact source cross-
reference). _time_stopped is pure and imported directly, unmodified.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tradepulse.models import AssetClass, Candle  # noqa: E402
from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price  # noqa: E402
from tradepulse.strategy import atr, compute_real_factors, signal_from_composite, weighted_composite  # noqa: E402
from tradepulse.config import default_strategy_weights  # noqa: E402

CACHE_ROOT = REPO_ROOT / "data" / "calibration"
MIN_CANDLES = 30
ATR_TRAIL_LOOKBACK_DAYS = 30  # matches monitor/coordinator.py::_fetch_atr's own 30-day lookback for the trailing-stop ATR

# Held fixed across the whole grid sweep -- production's own already-
# calibrated ATR entry-stop parameters (identical across every risk profile
# today, verified directly: atr_stop_multiplier=2, min/max_stop_distance_pct
# =0.5/25, stop_loss_pct fallback=8 -- the "balanced" profile's own values).
ENTRY_ATR_MULTIPLIER = Decimal("2")
ENTRY_MIN_STOP_DISTANCE_PCT = Decimal("0.5")
ENTRY_MAX_STOP_DISTANCE_PCT = Decimal("25")
ENTRY_FALLBACK_STOP_LOSS_PCT = Decimal("8")


@dataclass(frozen=True, slots=True)
class Entry:
    symbol: str
    asset_class: str
    entry_date: str
    entry_index: int  # index into the symbol's own bar array
    entry_price: Decimal
    initial_stop: Decimal
    signal: str


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    entry: Entry
    exit_date: str | None
    exit_price: Decimal | None
    exit_reason: str  # "stop" | "time_stop" | "censored"
    r_multiple: Decimal | None  # None only for censored trades


def _load_bars(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_candle(bar: dict) -> Candle:
    return Candle(date=bar["date"], open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"], volume=bar["volume"])


def _benchmark_closes_as_of(benchmark_bars: list[dict], as_of_date: str) -> list[Decimal]:
    """As-of-date lookup, NOT a positional slice -- every benchmark bar with
    date <= as_of_date, in order. This is what actually prevents a future
    benchmark observation from ever entering the computation, and what
    correctly handles the candidate/benchmark having different missing-bar
    patterns (equity holidays vs. crypto's 7-day calendar) instead of
    silently misaligning them by array position."""
    return [Decimal(b["close"]) for b in benchmark_bars if b["date"] <= as_of_date]


def generate_entries(symbol: str, asset_class: str, benchmark_bars: list[dict]) -> tuple[list[Entry], list[dict]]:
    """Walks the symbol's own bar array day-by-day, starting once MIN_CANDLES
    of trailing history exists. At day D, only candles[:D+1] and benchmark
    bars dated <= candles[D]'s date are ever visible -- lookahead is
    structurally impossible by construction (see test_calibration_harness.py
    for the automated proof this claim is tested, not just asserted here).
    Returns (entries, all_bars) -- all_bars is handed back so the caller can
    run exit simulation without re-reading the cache file."""
    data = _load_bars(CACHE_ROOT / asset_class / f"{symbol.replace('/', '-')}.json")
    bars = data["bars"]
    calendar = "crypto" if asset_class == "crypto" else "equity"
    weights = default_strategy_weights(datetime.now(UTC))

    entries: list[Entry] = []
    for i in range(MIN_CANDLES - 1, len(bars)):
        window = bars[max(0, i - 249) : i + 1]  # 200-trading-day-ish window, matching the scanner's own default lookback
        candles = [_to_candle(b) for b in window]
        as_of_date = bars[i]["date"]
        bench_closes = _benchmark_closes_as_of(benchmark_bars, as_of_date)
        scores = compute_real_factors(candles, calendar=calendar, benchmark_closes=bench_closes if bench_closes else None)
        if scores is None:
            continue
        composite = weighted_composite(scores, weights)
        signal = signal_from_composite(composite)
        if signal not in ("BUY", "STRONG_BUY"):
            continue

        entry_price = Decimal(bars[i]["close"])
        asset_cls_enum = AssetClass.CRYPTO if asset_class == "crypto" else AssetClass.EQUITY
        atr_stop = _atr_stop_loss_price(
            entry_price, candles, ENTRY_ATR_MULTIPLIER, asset_cls_enum,
            ENTRY_MIN_STOP_DISTANCE_PCT, ENTRY_MAX_STOP_DISTANCE_PCT,
        )
        initial_stop = atr_stop if atr_stop is not None else _stop_loss_price(entry_price, ENTRY_FALLBACK_STOP_LOSS_PCT, asset_cls_enum)
        entries.append(Entry(
            symbol=symbol, asset_class=asset_class, entry_date=as_of_date, entry_index=i,
            entry_price=entry_price, initial_stop=initial_stop, signal=signal,
        ))
    return entries, bars


def _ratchet_candidate_stop(
    average_price: Decimal, running_extreme: Decimal, atr_value: Decimal | None,
    trailing_atr_multiplier: Decimal, gain_pct: Decimal, break_even_trigger_pct: Decimal,
    current_stop: Decimal | None,
) -> Decimal | None:
    """Field-for-field mirror of monitor/coordinator.py::_ratchet_stop's
    math (lines ~181-224 as of the Rev.85 revert) -- that function is async
    and DB-coupled (reads/writes Holding.current_stop via
    repositories.holdings.mutate), so it can't be called directly from a
    pure simulation. Long-only (matches the scanner's BUY-only design, so
    is_long is never False here). Keep this in sync with _ratchet_stop if
    production's exit math ever changes."""
    if current_stop is None and gain_pct < break_even_trigger_pct:
        return current_stop  # break-even not yet earned
    candidate = average_price  # break-even floor, once earned, is never given back
    if atr_value is not None and trailing_atr_multiplier > 0:
        trail = running_extreme - atr_value * trailing_atr_multiplier
        candidate = max(candidate, trail)
    if current_stop is None:
        return candidate
    return max(current_stop, candidate)


def simulate_exit(
    entry: Entry, all_bars: list[dict], break_even_trigger_pct: Decimal, max_hold_days: int, trailing_atr_multiplier: Decimal,
) -> TradeOutcome:
    """Walks forward from entry.entry_index. Gap/fill rule (stated
    explicitly, not just implemented): if a day's open is already at/through
    the operative stop, the exit fills at that OPEN (a real gap-through is
    never assumed to fill at the stale stop price); otherwise, if the day's
    low touches the stop, the exit fills AT the stop (the standard
    daily-bar intraday-touch approximation). Time-stop fires when
    (days held) >= max_hold_days, checked calendar-day-wise against the
    entry date exactly as monitor/coordinator.py::_time_stopped does."""
    entry_date = date.fromisoformat(entry.entry_date)
    current_stop = entry.initial_stop
    running_extreme = entry.entry_price
    ratcheted_stop: Decimal | None = None  # None until break-even first earned, matching Holding.current_stop's own semantics

    for i in range(entry.entry_index + 1, len(all_bars)):
        bar = all_bars[i]
        bar_date = date.fromisoformat(bar["date"])
        open_px, low_px, close_px = Decimal(bar["open"]), Decimal(bar["low"]), Decimal(bar["close"])
        running_extreme = max(running_extreme, close_px)

        operative_stop = ratcheted_stop if ratcheted_stop is not None else current_stop
        if open_px <= operative_stop:
            return TradeOutcome(entry, bar["date"], open_px, "stop", _r_multiple(entry, open_px))
        if low_px <= operative_stop:
            return TradeOutcome(entry, bar["date"], operative_stop, "stop", _r_multiple(entry, operative_stop))

        if (bar_date - entry_date).days >= max_hold_days:
            return TradeOutcome(entry, bar["date"], close_px, "time_stop", _r_multiple(entry, close_px))

        gain_pct = (close_px - entry.entry_price) / entry.entry_price * 100
        atr_value: Decimal | None = None
        if i >= ATR_TRAIL_LOOKBACK_DAYS:
            window = all_bars[max(0, i - ATR_TRAIL_LOOKBACK_DAYS + 1) : i + 1]
            raw = atr([float(b["high"]) for b in window], [float(b["low"]) for b in window], [float(b["close"]) for b in window])
            atr_value = Decimal(str(raw)) if raw is not None else None
        ratcheted_stop = _ratchet_candidate_stop(
            entry.entry_price, running_extreme, atr_value, trailing_atr_multiplier, gain_pct, break_even_trigger_pct, ratcheted_stop,
        )

    return TradeOutcome(entry, None, None, "censored", None)


def _r_multiple(entry: Entry, exit_price: Decimal) -> Decimal:
    risk = entry.entry_price - entry.initial_stop
    if risk <= 0:
        return Decimal("0")
    return (exit_price - entry.entry_price) / risk
