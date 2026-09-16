"""Regression coverage for conservative daily-OHLC milestone ordering."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

from target_price_evidence import (  # noqa: E402
    Entry, MilestoneResult, _milestone_evidence, simulate_exit, simulate_exit_with_target,
)


@pytest.mark.parametrize(
    "prior_high,exit_open,exit_high,exit_low,prior_close,max_days,reason,reached,bars_to_reach",
    [
        ("110", "100", "111", "90", "100", 30, "stop", True, 1),
        ("109", "100", "110", "90", "100", 30, "stop", False, None),
        ("109", "89", "110", "88", "100", 30, "stop", False, None),
        ("109", "105", "110", "99", "105", 30, "stop", False, None),
        ("109", "100", "109", "90", "100", 30, "stop", False, None),
        ("109", "100", "110", "99", "100", 2, "time_stop", True, 2),
        ("109", "100", "110", "99", "100", 30, "censored", True, 2),
    ],
    ids=["before-exit", "same-bar-stop", "gap-stop", "break-even-stop",
         "never-reached", "time-stop-at-close", "censored-final-bar"],
)
def test_milestone_stop_before_target(
    prior_high, exit_open, exit_high, exit_low, prior_close, max_days,
    reason, reached, bars_to_reach,
) -> None:
    entry = Entry("X", "equity", "2024-01-01", 0, Decimal("100"), Decimal("90"), "BUY")
    bars = [
        {"date": "2024-01-01", "open": "100", "high": "100", "low": "100", "close": "100"},
        {"date": "2024-01-02", "open": "100", "high": prior_high, "low": "99", "close": prior_close},
        {"date": "2024-01-03", "open": exit_open, "high": exit_high, "low": exit_low, "close": "100"},
    ]
    baseline = simulate_exit(entry, bars, Decimal("4"), max_days, Decimal("2.5"))
    assert baseline.exit_reason == reason
    result = _milestone_evidence(
        entry, bars, baseline, Decimal("1"), {bar["date"]: i for i, bar in enumerate(bars)},
    )
    assert result == MilestoneResult(reached, bars_to_reach, baseline.r_multiple)
    target = simulate_exit_with_target(entry, bars, Decimal("4"), max_days, Decimal("2.5"), Decimal("1"))
    if reached:
        assert target.exit_reason == "target_price"
        assert target.exit_date == bars[bars_to_reach]["date"]
    else:
        assert target == baseline
