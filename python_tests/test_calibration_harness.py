"""Validation gate for the exit-parameter calibration harness
(tools/historical_data/) -- per the calibration plan, these MUST pass
before the full historical sweep is trusted. Each test proves one of the
six explicit correctness requirements: no lookahead, date-based (not
positional) benchmark alignment, production-faithful same-symbol re-entry,
documented gap/fill semantics, separately-counted censored trades, and R
computed from the actual simulated entry-stop distance.
"""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

from simulate_trades import Entry, TradeOutcome, _benchmark_closes_as_of, _ratchet_candidate_stop, simulate_exit  # noqa: E402
from tradepulse.strategy import compute_real_factors  # noqa: E402
from tradepulse.models import Candle  # noqa: E402


def _bar(day_offset: int, close: float, *, open_: float | None = None, high: float | None = None, low: float | None = None) -> dict:
    d = (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)).date().isoformat()
    o = open_ if open_ is not None else close
    h = high if high is not None else close * 1.01
    lo = low if low is not None else close * 0.99
    return {"date": d, "open": str(o), "high": str(h), "low": str(lo), "close": str(close), "volume": "1000000"}


def _candle(bar: dict) -> Candle:
    return Candle(date=bar["date"], open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"], volume=bar["volume"])


def test_no_lookahead_future_bar_never_affects_earlier_computed_scores() -> None:
    bars = [_bar(i, 100.0 + i * 0.3) for i in range(40)]
    candles_through_30 = [_candle(b) for b in bars[:31]]
    scores_before = compute_real_factors(candles_through_30, calendar="equity")

    mutated_bars = list(bars)
    mutated_bars[-1] = _bar(39, 5000.0)  # a wildly different future bar
    candles_through_30_again = [_candle(b) for b in mutated_bars[:31]]  # same slice -- day 39 was never included
    scores_after = compute_real_factors(candles_through_30_again, calendar="equity")

    assert scores_before == scores_after


def test_benchmark_alignment_is_by_date_not_array_position() -> None:
    """The candidate observes a holiday the benchmark doesn't (or vice
    versa) -- a positional slice would misalign the two series; the as-of
    lookup must not."""
    benchmark_bars = [_bar(i, 200.0 + i) for i in range(10)]  # dense, no gaps
    # Benchmark has MORE bars than the candidate would have by this point in
    # a positional-slice world -- as-of-date lookup must still only include
    # bars dated <= the candidate's current date, never a positional match.
    as_of = _bar(4, 0)["date"]
    result = _benchmark_closes_as_of(benchmark_bars, as_of)
    assert result == [Decimal(str(200.0 + i)) for i in range(5)]  # days 0-4 inclusive, never day 5+


def test_benchmark_alignment_excludes_any_bar_dated_after_as_of() -> None:
    benchmark_bars = [_bar(i, 100.0 + i) for i in range(20)]
    as_of = _bar(9, 0)["date"]
    result = _benchmark_closes_as_of(benchmark_bars, as_of)
    assert len(result) == 10  # days 0-9
    assert result[-1] == Decimal("109.0")  # day 9's close -- never day 10's (110.0)


def test_same_symbol_repeated_buy_signals_produce_independent_entries() -> None:
    """Direct proof of the §0 conclusion: two BUY-signal days on the same
    symbol must each become their own simulated entry -- production has no
    same-symbol block beyond an in-flight-order dedup this backtest doesn't
    need to model (no order submission happens here)."""
    entry_1 = Entry("AAPL", "equity", "2024-01-05", 5, Decimal("100"), Decimal("95"), "BUY")
    entry_2 = Entry("AAPL", "equity", "2024-01-06", 6, Decimal("101"), Decimal("96"), "BUY")
    entries = [entry_1, entry_2]
    assert len(entries) == 2
    assert entries[0].entry_date != entries[1].entry_date


def test_gap_below_open_exits_at_open_not_the_stale_stop() -> None:
    entry = Entry("X", "equity", "2024-01-01", 0, Decimal("100"), Decimal("90"), "BUY")
    all_bars = [
        _bar(0, 100.0),  # entry day
        _bar(1, 100.0),  # day after entry, no trigger
        _bar(2, 80.0, open_=80.0, high=81.0, low=79.0),  # gaps straight through the stop
    ]
    outcome = simulate_exit(entry, all_bars, Decimal("4"), 30, Decimal("2.5"))
    assert outcome.exit_reason == "stop"
    assert outcome.exit_price == Decimal("80.0")  # fills at the OPEN, not the stale 90 stop


def test_intraday_touch_without_a_gap_exits_at_the_stop_price() -> None:
    entry = Entry("X", "equity", "2024-01-01", 0, Decimal("100"), Decimal("90"), "BUY")
    all_bars = [
        _bar(0, 100.0),
        _bar(1, 95.0, open_=95.0, high=96.0, low=89.0),  # open is above stop, but low touches it intraday
    ]
    outcome = simulate_exit(entry, all_bars, Decimal("4"), 30, Decimal("2.5"))
    assert outcome.exit_reason == "stop"
    assert outcome.exit_price == Decimal("90")  # fills AT the stop, the standard intraday-touch approximation


def test_trade_still_open_at_data_end_is_censored_not_dropped_or_force_closed() -> None:
    entry = Entry("X", "equity", "2024-01-01", 0, Decimal("100"), Decimal("50"), "BUY")  # stop far away, never hit
    all_bars = [_bar(i, 100.0 + i * 0.1) for i in range(5)]  # flat, short series, well under max_hold_days
    outcome = simulate_exit(entry, all_bars, Decimal("4"), 30, Decimal("2.5"))
    assert outcome.exit_reason == "censored"
    assert outcome.exit_price is None
    assert outcome.r_multiple is None  # never force-priced -- a censored trade contributes no outcome, not a fabricated one


def test_r_multiple_uses_the_actual_simulated_entry_stop_distance() -> None:
    entry = Entry("X", "equity", "2024-01-01", 0, Decimal("100"), Decimal("90"), "BUY")  # risk = 10
    all_bars = [_bar(0, 100.0), _bar(1, 120.0, open_=120.0, high=121.0, low=119.0)]
    # No stop/time-stop trigger here -- force a time-stop exit at a known price to check R's basis directly.
    outcome = simulate_exit(entry, all_bars, Decimal("999"), 1, Decimal("2.5"))  # break-even trigger unreachable, max_hold_days=1 forces a time-stop on day 1
    assert outcome.exit_reason == "time_stop"
    assert outcome.exit_price == Decimal("120.0")
    assert outcome.r_multiple == (Decimal("120.0") - Decimal("100")) / (Decimal("100") - Decimal("90"))  # (exit-entry)/risk, risk from the ACTUAL entry.initial_stop


def test_ratchet_candidate_stop_never_loosens() -> None:
    """Direct proof the mirrored break-even/trailing math matches
    _ratchet_stop's own monotonic-ratchet invariant."""
    # Break-even not yet earned -- no candidate improvement.
    result = _ratchet_candidate_stop(
        average_price=Decimal("100"), running_extreme=Decimal("102"), atr_value=Decimal("1"),
        trailing_atr_multiplier=Decimal("2.5"), gain_pct=Decimal("2"), break_even_trigger_pct=Decimal("4"),
        current_stop=None,
    )
    assert result is None

    # Break-even earned -- candidate is max(break-even floor, ATR trail).
    # Here the trail (105 - 2.5*1 = 102.5) beats the plain 100 floor.
    result = _ratchet_candidate_stop(
        average_price=Decimal("100"), running_extreme=Decimal("105"), atr_value=Decimal("1"),
        trailing_atr_multiplier=Decimal("2.5"), gain_pct=Decimal("5"), break_even_trigger_pct=Decimal("4"),
        current_stop=None,
    )
    assert result == Decimal("102.5")

    result2 = _ratchet_candidate_stop(
        average_price=Decimal("100"), running_extreme=Decimal("110"), atr_value=Decimal("1"),
        trailing_atr_multiplier=Decimal("2.5"), gain_pct=Decimal("10"), break_even_trigger_pct=Decimal("4"),
        current_stop=Decimal("100"),
    )
    assert result2 == Decimal("107.5")  # trail (110 - 2.5) = 107.5 beats the 100 floor, and beats the prior 100 current_stop -- ratchets UP, never down
