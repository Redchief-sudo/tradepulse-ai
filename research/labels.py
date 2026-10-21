"""Outcome labelling: score every AI-scanned candidate by what its price did next.

Offline research only (docs/intelligence-roadmap.md, item 1). Reads a COPY of
a trading or soak database, never the original, and writes to its own
labels database. Lives outside tradepulse/, so it is never part of a soak's
frozen source and cannot change trading behaviour.

Sample unit: one candidate-day = (lane, symbol, New York trading date), keeping
the first scan of the day. Entry is the decision day's close. Labels are
written only once their horizon has fully passed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

LABEL_VERSION = "v1"
HORIZONS = (1, 5, 10)
MAX_HOLD_BARS = 15
STALL_BARS = 7
STALL_MIN_R = Decimal("0.5")
SCALE_OUT_R = Decimal("1")
FINAL_TARGET_R = Decimal("3")
# Round-trip cost as % of entry: the soaks' frozen overlay (25 bps fees +
# 15 bps slippage), plus Alpaca's entry-tier crypto taker fee on both sides.
COST_PCT = {"equity": Decimal("0.40"), "option": Decimal("0.40"), "crypto": Decimal("0.90")}
BENCHMARK = {"equity": "SPY", "option": "SPY", "crypto": "BTC/USD"}
_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(slots=True)
class CandidateDay:
    lane: str
    symbol: str
    decision_date: date
    decision_at: str
    ai_recommendation: str | None
    ai_confidence: str | None
    ai_model: str | None
    gate: str
    scans_that_day: int = 1
    any_accepted: bool = False
    regime: dict = field(default_factory=dict)
    factor_scores: dict | None = None

    def key(self, source_id: str) -> str:
        return f"{LABEL_VERSION}|{source_id}|{self.lane}|{self.symbol}|{self.decision_date.isoformat()}"


# ---- reading a source database ------------------------------------------------

def snapshot(source: Path, destination: Path) -> None:
    """Copy a live database without writing to it (read-only connection, SQLite backup)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def source_identity(connection: sqlite3.Connection, fallback: str) -> str:
    try:
        row = connection.execute("SELECT database_id FROM verification_identity LIMIT 1").fetchone()
    except sqlite3.Error:
        row = None
    return row[0] if row and row[0] else fallback


def _payloads(connection: sqlite3.Connection, table: str) -> list[dict]:
    try:
        return [json.loads(row[0]) for row in connection.execute(f"SELECT payload FROM {table} ORDER BY rowid")]
    except sqlite3.OperationalError:
        return []


def ny_date(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).astimezone(_NY).date()


def candidate_days(connection: sqlite3.Connection) -> list[CandidateDay]:
    """Every AI candidate, joined to its scan (lane, time, regime) and its gate
    outcome, deduplicated to one record per (lane, symbol, NY date)."""
    scans = {s.get("ai_response_request_id"): s for s in _payloads(connection, "scan_runs") if s.get("ai_response_request_id")}
    rejections = {}
    for r in _payloads(connection, "rejected_candidates"):
        rejections.setdefault((r.get("scan_run_id"), r.get("symbol")), r)
    accepted = {}
    for o in _payloads(connection, "opportunities"):
        request_id = (o.get("metadata") or {}).get("ai_request_id")
        accepted[(request_id, (o.get("asset") or {}).get("symbol"))] = o
    days: dict[tuple, CandidateDay] = {}
    for response in _payloads(connection, "ai_responses"):
        scan = scans.get(response.get("request_id"))
        if scan is None:
            continue
        lane, decided_at = scan.get("asset_class"), scan.get("started_at")
        if not lane or not decided_at:
            continue
        regime = {k: scan.get(k) for k in ("regime", "regime_reason", "regime_confidence", "regime_position_multiplier",
                                           "regime_realized_vol", "regime_weight_profile")}
        for candidate in (response.get("result") or {}).get("candidates") or []:
            symbol = candidate.get("symbol")
            if not symbol:
                continue
            opportunity = accepted.get((response.get("request_id"), symbol))
            rejection = rejections.get((scan.get("scan_run_id"), symbol))
            gate = "accepted" if opportunity else (rejection.get("reason") if rejection else "not_recorded")
            factors = ((opportunity.get("metadata") or {}).get("factor_scores") if opportunity
                       else ((rejection or {}).get("context") or {}).get("factor_scores"))
            day_key = (lane, symbol, ny_date(decided_at))
            existing = days.get(day_key)
            if existing is not None:
                existing.scans_that_day += 1
                existing.any_accepted = existing.any_accepted or gate == "accepted"
                if existing.factor_scores is None and factors:
                    existing.factor_scores = factors
                continue
            confidence = candidate.get("confidence")
            days[day_key] = CandidateDay(
                lane=lane, symbol=symbol, decision_date=day_key[2], decision_at=decided_at,
                ai_recommendation=candidate.get("recommendation"),
                ai_confidence=str(confidence) if confidence is not None else None,
                ai_model=response.get("model"), gate=gate, any_accepted=gate == "accepted",
                regime=regime, factor_scores=factors,
            )
    return sorted(days.values(), key=lambda d: (d.decision_at, d.lane, d.symbol))


# ---- labels ---------------------------------------------------------------------

def _entry_index(bars: Sequence[Bar], decision_date: date) -> int | None:
    for index, bar in enumerate(bars):
        if bar.day >= decision_date:
            return index
    return None


def forward_returns(bars: Sequence[Bar], decision_date: date) -> dict | None:
    """Returns from the decision day's close over 1/5/10 bars, plus the best
    and worst prices within 10. None until the longest horizon has passed."""
    start = _entry_index(bars, decision_date)
    if start is None or start + max(HORIZONS) >= len(bars):
        return None
    entry = bars[start].close
    window = bars[start + 1:start + 1 + max(HORIZONS)]
    labels = {f"ret_{h}d": str(bars[start + h].close / entry - 1) for h in HORIZONS}
    labels["mfe_10d"] = str(max(b.high for b in window) / entry - 1)
    labels["mae_10d"] = str(min(b.low for b in window) / entry - 1)
    labels["entry_price"] = str(entry)
    labels["entry_date"] = bars[start].day.isoformat()
    return labels


def replay_ladder(entry: Decimal, stop: Decimal, after: Sequence[Bar], *, cost_r: Decimal = Decimal(0)) -> dict | None:
    """Replay the Phase 1 exit ladder on daily bars after entry, in R.

    Per bar, in conservative order: a gap through the stop exits at the open;
    the stop (or break-even, once scaled out) is checked before any target,
    so a bar touching both counts as stopped; at +1R half is sold and the
    stop moves to break-even, and if that same bar also reaches break-even
    the rest exits there; the rest exits at +3R. Not yet scaled out and held
    STALL_BARS bars without the best high reaching +0.5R: exit at the close.
    Otherwise exit at the close of bar MAX_HOLD_BARS. The ATR trailing stop
    is not replayed in v1. None if there are not enough bars yet."""
    risk = entry - stop
    if risk <= 0 or len(after) < MAX_HOLD_BARS:
        return None
    level, target, remaining, result, scaled, best = stop, entry + FINAL_TARGET_R * risk, Decimal(1), Decimal(0), False, entry

    def r(price: Decimal) -> Decimal:
        return (price - entry) / risk

    for held, bar in enumerate(after[:MAX_HOLD_BARS], start=1):
        if bar.open <= level:
            return {"r": str(result + remaining * r(bar.open) - cost_r), "exit": "gap_stop" if not scaled else "gap_break_even", "bars": held}
        if bar.low <= level:
            return {"r": str(result + remaining * r(level) - cost_r), "exit": "stop" if not scaled else "break_even", "bars": held}
        if not scaled and bar.high >= entry + SCALE_OUT_R * risk:
            result += remaining / 2 * SCALE_OUT_R
            remaining /= 2
            scaled, level = True, max(level, entry)
            if bar.low <= level:
                return {"r": str(result + remaining * r(level) - cost_r), "exit": "break_even", "bars": held}
        if scaled and bar.high >= target:
            return {"r": str(result + remaining * FINAL_TARGET_R - cost_r), "exit": "target_3r", "bars": held}
        best = max(best, bar.high)
        if not scaled and held >= STALL_BARS and r(best) < STALL_MIN_R:
            return {"r": str(result + remaining * r(bar.close) - cost_r), "exit": "stall", "bars": held}
    last = after[MAX_HOLD_BARS - 1]
    return {"r": str(result + remaining * r(last.close) - cost_r), "exit": "time_stop", "bars": MAX_HOLD_BARS}


StopRule = Callable[[Decimal, Sequence[Bar], str], Decimal | None]


def label_candidate(candidate: CandidateDay, bars: Sequence[Bar], benchmark_bars: Sequence[Bar] | None,
                    stop_rule: StopRule) -> dict | None:
    """All labels for one candidate-day, or None while any horizon is unfinished."""
    forward = forward_returns(bars, candidate.decision_date)
    if forward is None:
        return None
    start = _entry_index(bars, candidate.decision_date)
    entry = Decimal(forward["entry_price"])
    stop = stop_rule(entry, bars[:start + 1], candidate.lane)
    labels = dict(forward)
    if stop is not None and stop < entry:
        cost_r = COST_PCT.get(candidate.lane, Decimal("0.40")) / 100 * entry / (entry - stop)
        replay = replay_ladder(entry, stop, bars[start + 1:], cost_r=cost_r)
        if replay is None:
            return None
        labels.update({"r_outcome": replay["r"], "r_exit": replay["exit"], "r_bars": replay["bars"],
                       "stop": str(stop), "risk_pct": str((entry - stop) / entry * 100)})
    else:
        labels.update({"r_outcome": None, "r_exit": "no_stop", "r_bars": None})
    if benchmark_bars:
        bench = forward_returns(benchmark_bars, candidate.decision_date)
        labels["benchmark_ret_5d"] = bench["ret_5d"] if bench else None
    return labels


# ---- labels database ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  key TEXT PRIMARY KEY, label_version TEXT NOT NULL, source_id TEXT NOT NULL, lane TEXT NOT NULL,
  symbol TEXT NOT NULL, decision_date TEXT NOT NULL, decision_at TEXT NOT NULL,
  features TEXT NOT NULL, labels TEXT, status TEXT NOT NULL, labelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status, lane, symbol);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, sources TEXT NOT NULL, counts TEXT
);
"""


def open_labels(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def features_of(candidate: CandidateDay) -> dict:
    return {"ai_recommendation": candidate.ai_recommendation, "ai_confidence": candidate.ai_confidence,
            "ai_model": candidate.ai_model, "gate": candidate.gate, "any_accepted": candidate.any_accepted,
            "scans_that_day": candidate.scans_that_day, "regime": candidate.regime,
            "factor_scores": candidate.factor_scores,
            "factors_source": "stored" if candidate.factor_scores else "missing"}


def record_candidates(labels_db: sqlite3.Connection, source_id: str, candidates: Sequence[CandidateDay]) -> int:
    """Insert new candidate-days as pending; existing rows are never rewritten."""
    added = 0
    for candidate in candidates:
        cursor = labels_db.execute(
            "INSERT OR IGNORE INTO candidates(key,label_version,source_id,lane,symbol,decision_date,decision_at,features,status)"
            " VALUES (?,?,?,?,?,?,?,?,'pending')",
            (candidate.key(source_id), LABEL_VERSION, source_id, candidate.lane, candidate.symbol,
             candidate.decision_date.isoformat(), candidate.decision_at, json.dumps(features_of(candidate), sort_keys=True)))
        added += cursor.rowcount
    labels_db.commit()
    return added


def fill_pending(labels_db: sqlite3.Connection, bars_for: Callable[[str, str], Sequence[Bar]],
                 stop_rule: StopRule) -> dict:
    """Label every pending row whose horizons have passed. bars_for(symbol, lane)
    returns daily bars covering the decision dates (an empty list = no data)."""
    counts = {"labelled": 0, "still_pending": 0, "no_data": 0}
    rows = labels_db.execute("SELECT key, lane, symbol, decision_date, decision_at, features FROM candidates "
                             "WHERE status='pending' AND label_version=?", (LABEL_VERSION,)).fetchall()
    for key, lane, symbol, decision_date, decision_at, features in rows:
        bars = bars_for(symbol, lane)
        if not bars:
            labels_db.execute("UPDATE candidates SET status='no_data', labelled_at=? WHERE key=?",
                              (datetime.now(UTC).isoformat(), key))
            counts["no_data"] += 1
            continue
        benchmark = BENCHMARK.get(lane)
        benchmark_bars = bars_for(benchmark, "crypto" if benchmark == "BTC/USD" else "equity") if benchmark else None
        f = json.loads(features)
        candidate = CandidateDay(lane=lane, symbol=symbol, decision_date=date.fromisoformat(decision_date),
                                 decision_at=decision_at, ai_recommendation=f.get("ai_recommendation"),
                                 ai_confidence=f.get("ai_confidence"), ai_model=f.get("ai_model"), gate=f.get("gate"))
        labels = label_candidate(candidate, bars, benchmark_bars, stop_rule)
        if labels is None:
            counts["still_pending"] += 1
            continue
        labels_db.execute("UPDATE candidates SET labels=?, status='labelled', labelled_at=? WHERE key=? AND status='pending'",
                          (json.dumps(labels, sort_keys=True), datetime.now(UTC).isoformat(), key))
        counts["labelled"] += 1
    labels_db.commit()
    return counts
