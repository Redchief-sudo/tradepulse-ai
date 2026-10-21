"""Outcome labelling (research/labels.py): replay rules, maturity, extraction, idempotence, read-only sources."""
import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from research.labels import (
    Bar,
    candidate_days,
    fill_pending,
    forward_returns,
    open_labels,
    record_candidates,
    replay_ladder,
    snapshot,
)

D0 = date(2026, 10, 1)


def bars(rows, start=D0):
    """rows: (open, high, low, close) per day starting at `start`."""
    return [Bar(start + timedelta(days=i), *(Decimal(str(v)) for v in row)) for i, row in enumerate(rows)]


FLAT = (100.5, 101, 100, 100.5)  # never reaches +0.5R (101.5) for entry 100 / stop 97


def pad(rows, n=15, fill=FLAT):
    return list(rows) + [fill] * (n - len(rows))


# ---- replay -------------------------------------------------------------------

def test_stop_is_minus_one_r_and_costs_are_subtracted():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([(99, 99.5, 96.5, 97.5)])), cost_r=Decimal("0.1"))
    assert (Decimal(out["r"]), out["exit"], out["bars"]) == (Decimal("-1.1"), "stop", 1)


def test_a_gap_through_the_stop_exits_at_the_open():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([(95, 96, 94, 95.5)])))
    assert out["exit"] == "gap_stop" and Decimal(out["r"]) == (Decimal(95) - 100) / 3


def test_a_bar_touching_both_stop_and_one_r_counts_as_stopped():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([(100, 104, 96, 101)])))
    assert (Decimal(out["r"]), out["exit"]) == (Decimal(-1), "stop")


def test_scale_out_then_break_even_in_the_same_bar_is_half_r():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([(100.5, 103, 99, 101)])))
    assert (Decimal(out["r"]), out["exit"]) == (Decimal("0.5"), "break_even")


def test_scale_out_then_three_r_is_two_r():
    rows = [(100.5, 103, 101, 102.5), (102.5, 105, 102, 104), (104, 109, 103.5, 108)]
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad(rows, fill=(108, 108.5, 107, 108))))
    assert (Decimal(out["r"]), out["exit"], out["bars"]) == (Decimal(2), "target_3r", 3)


def test_a_stalled_trade_exits_at_the_seventh_close():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([], fill=(100.4, 101.4, 99, 100.5))))
    assert (out["exit"], out["bars"]) == ("stall", 7) and Decimal(out["r"]) == Decimal("0.5") / 3


def test_otherwise_the_time_stop_exits_at_the_fifteenth_close():
    out = replay_ladder(Decimal(100), Decimal(97), bars(pad([], fill=(101.5, 102.5, 99.5, 102))))
    assert (out["exit"], out["bars"]) == ("time_stop", 15) and Decimal(out["r"]) == Decimal(2) / 3


def test_replay_waits_for_enough_bars():
    assert replay_ladder(Decimal(100), Decimal(97), bars([FLAT] * 14)) is None


# ---- forward returns ----------------------------------------------------------

def test_forward_returns_only_after_the_longest_horizon():
    series = bars([(100, 101, 99, 100)] + [(100 + i, 100 + i + 1, 100 + i - 1, 100 + i) for i in range(1, 11)])
    assert forward_returns(series[:10], D0) is None  # 9 bars after entry: not mature
    labels = forward_returns(series, D0)
    assert Decimal(labels["ret_1d"]) == Decimal("0.01") and Decimal(labels["ret_10d"]) == Decimal("0.1")
    assert Decimal(labels["mfe_10d"]) == Decimal("0.11") and labels["entry_date"] == D0.isoformat()


# ---- extraction from a trading database ---------------------------------------

def _source_db(path):
    connection = sqlite3.connect(path)
    for table in ("scan_runs", "ai_responses", "rejected_candidates", "opportunities"):
        connection.execute(f"CREATE TABLE {table} (record_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def put(table, record_id, payload):
        connection.execute(f"INSERT INTO {table} VALUES (?,?)", (record_id, json.dumps(payload)))

    for scan_id, request_id, started in (("s1", "r1", "2026-10-01T14:00:00+00:00"), ("s2", "r2", "2026-10-01T15:00:00+00:00")):
        put("scan_runs", scan_id, {"scan_run_id": scan_id, "ai_response_request_id": request_id, "asset_class": "equity",
                                   "started_at": started, "regime": "trend"})
        put("ai_responses", request_id, {"request_id": request_id, "model": "m", "result": {"candidates": [
            {"symbol": "AAPL", "recommendation": "BUY", "confidence": 78.0},
            {"symbol": "MSFT", "recommendation": "BUY", "confidence": 85.0}]}})
    put("rejected_candidates", "x1", {"scan_run_id": "s1", "symbol": "AAPL", "reason": "CONFIDENCE_BELOW_MIN", "context": {}})
    put("rejected_candidates", "x2", {"scan_run_id": "s1", "symbol": "MSFT", "reason": "DETERMINISTIC_SIGNAL_DISAGREED",
                                      "context": {"factor_scores": {"price": "500"}}})
    put("opportunities", "o1", {"asset": {"symbol": "MSFT"}, "metadata": {"ai_request_id": "r2", "factor_scores": {"price": "501"}}})
    connection.commit()
    connection.close()


def test_candidates_dedupe_to_one_per_lane_symbol_and_ny_day(tmp_path):
    _source_db(tmp_path / "src.db")
    days = candidate_days(sqlite3.connect(tmp_path / "src.db"))
    by_symbol = {d.symbol: d for d in days}
    assert len(days) == 2 and set(by_symbol) == {"AAPL", "MSFT"}
    msft = by_symbol["MSFT"]
    assert (msft.gate, msft.scans_that_day, msft.any_accepted) == ("DETERMINISTIC_SIGNAL_DISAGREED", 2, True)
    assert msft.factor_scores == {"price": "500"}  # the first scan's stored factors
    assert (by_symbol["AAPL"].gate, by_symbol["AAPL"].ai_confidence) == ("CONFIDENCE_BELOW_MIN", "78.0")


def test_snapshot_never_changes_the_source(tmp_path):
    _source_db(tmp_path / "src.db")
    before = hashlib.sha256((tmp_path / "src.db").read_bytes()).hexdigest()
    snapshot(tmp_path / "src.db", tmp_path / "work" / "copy.db")
    assert hashlib.sha256((tmp_path / "src.db").read_bytes()).hexdigest() == before
    assert len(candidate_days(sqlite3.connect(tmp_path / "work" / "copy.db"))) == 2


def test_labelling_is_idempotent_and_leaves_immature_rows_pending(tmp_path):
    _source_db(tmp_path / "src.db")
    days = candidate_days(sqlite3.connect(tmp_path / "src.db"))
    labels_db = open_labels(tmp_path / "labels.sqlite")
    assert record_candidates(labels_db, "src", days) == 2
    assert record_candidates(labels_db, "src", days) == 0  # never re-added
    series = bars([(100, 101, 99, 100)] * 30)

    def stop_rule(entry, history, lane):
        return entry - Decimal(3)

    assert fill_pending(labels_db, lambda s, lane: series[:5], stop_rule)["still_pending"] == 2
    first = fill_pending(labels_db, lambda s, lane: series, stop_rule)
    assert first["labelled"] == 2
    labelled = labels_db.execute("SELECT labels FROM candidates WHERE symbol='AAPL'").fetchone()[0]
    assert fill_pending(labels_db, lambda s, lane: series, stop_rule)["labelled"] == 0  # nothing rewritten
    assert labels_db.execute("SELECT labels FROM candidates WHERE symbol='AAPL'").fetchone()[0] == labelled
    assert json.loads(labelled)["r_exit"] == "stall"
