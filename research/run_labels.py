"""Nightly outcome labelling: snapshot sources, record candidate-days, label matured ones.

    .venv/bin/python -m research.run_labels --database data/soaks/<run>/soak-accounting-1.preserved.sqlite \
        [--database ...] [--out data/research/labels.sqlite]

Read-only against Alpaca (daily bars) and against every source database
(copied first). Writes only to --out and its working copies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from research.labels import (
    Bar,
    candidate_days,
    fill_pending,
    open_labels,
    record_candidates,
    snapshot,
    source_identity,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "data/research/labels.sqlite"
LOOKBACK_DAYS = 120  # enough daily history for ATR(14) at the earliest decision


def stop_rule_for_profile(profile: str = "balanced"):
    """The scanner's own entry stop: 2x ATR within the profile's sanity band,
    else the fixed stop percentage (scanner/coordinator.py). The options lane
    is labelled on its underlying with the equity rule."""
    from tradepulse.config import risk_limits_for_profile
    from tradepulse.models import AssetClass, Candle
    from tradepulse.scanner.coordinator import _atr_stop_loss_price, _stop_loss_price

    limits = risk_limits_for_profile(profile)

    def rule(entry: Decimal, history, lane: str) -> Decimal | None:
        asset_class = AssetClass.CRYPTO if lane == "crypto" else AssetClass.EQUITY
        candles = [Candle(date=b.day.isoformat(), open=b.open, high=b.high, low=b.low, close=b.close, volume=Decimal(0))
                   for b in history]
        stop = _atr_stop_loss_price(entry, candles, limits.atr_stop_multiplier, asset_class,
                                    limits.min_stop_distance_pct, limits.max_stop_distance_pct) if candles else None
        return stop or (_stop_loss_price(entry, limits.stop_loss_pct, asset_class) if limits.stop_loss_pct > 0 else None)

    return rule


class AlpacaBars:
    """Cached daily bars per (symbol, asset class) over one date range."""

    def __init__(self, client, start: date, end: date):
        self._client, self._start, self._end = client, start, end
        self._cache: dict[tuple[str, str], list[Bar]] = {}

    async def load(self, symbol: str, lane: str) -> list[Bar]:
        from tradepulse.models import AssetClass

        asset_class = AssetClass.CRYPTO if lane == "crypto" else AssetClass.EQUITY
        key = (symbol, asset_class.value)
        if key not in self._cache:
            start = datetime.combine(self._start, datetime.min.time(), UTC)
            end = datetime.combine(self._end, datetime.min.time(), UTC)
            try:
                raw = await self._client.get_bars(symbol, asset_class, start, end, limit=1000)
            except Exception:  # noqa: BLE001 - a symbol without data is recorded as no_data, not a failed run
                raw = []
            bars = sorted((Bar(day=date.fromisoformat(r.date[:10]), open=r.open, high=r.high, low=r.low, close=r.close)
                           for r in raw), key=lambda b: b.day)
            self._cache[key] = bars
        return self._cache[key]


async def run(databases: list[Path], out: Path) -> dict:
    from tradepulse.cli import _load_dotenv
    from tradepulse.config import Settings
    from tradepulse.session_commands import build_broker

    work = out.parent / "work"
    labels_db = open_labels(out)
    run_id, started = str(uuid4()), datetime.now(UTC).isoformat()
    sources, added = [], 0
    for database in databases:
        copy = work / f"{database.parent.name}-{database.name}"
        snapshot(database, copy)
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            source_id = source_identity(connection, fallback=str(database))
            added += record_candidates(labels_db, source_id, candidate_days(connection))
        finally:
            connection.close()
        sources.append({"path": str(database), "source_id": source_id})
    pending = labels_db.execute("SELECT min(decision_date), max(decision_date) FROM candidates WHERE status='pending'").fetchone()
    counts = {"added": added, "labelled": 0, "still_pending": 0, "no_data": 0}
    if pending[0]:
        _load_dotenv()
        client = build_broker(Settings.from_env())
        try:
            bars = AlpacaBars(client, date.fromisoformat(pending[0]) - timedelta(days=LOOKBACK_DAYS),
                              datetime.now(UTC).date() + timedelta(days=1))
            needed = labels_db.execute("SELECT DISTINCT symbol, lane FROM candidates WHERE status='pending'").fetchall()
            for symbol, lane in [*needed, ("SPY", "equity"), ("BTC/USD", "crypto")]:
                await bars.load(symbol, lane)
            counts.update(fill_pending(labels_db, lambda s, lane: bars._cache.get(
                (s, "crypto" if lane == "crypto" else "equity"), []), stop_rule_for_profile()))
        finally:
            await client.aclose()
    labels_db.execute("INSERT INTO runs(run_id,started_at,finished_at,sources,counts) VALUES (?,?,?,?,?)",
                      (run_id, started, datetime.now(UTC).isoformat(), json.dumps(sources), json.dumps(counts)))
    labels_db.commit()
    labels_db.close()
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    counts = asyncio.run(run(args.database, args.out))
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
