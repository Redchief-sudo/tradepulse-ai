"""Build the Rev.140 legacy short-lot fixtures with the pre-Rev.140 code (c10bb2fa).

    python python_tests/fixtures/rev140/make_legacy_short_fixture.py [--commit c10bb2fa]

It exports that commit's ``tradepulse`` package into a scratch directory and
runs its own SettlementProcessor on an oversell, so the short lot is
authentic old-code output. Two databases are written next to this script:

- ``legacy_short_open.db``: buy 10 at 100, then a sell of 12 at 110. The
  old code closes the long lot and opens a 2-share short lot.
- ``legacy_short_closed.db``: the same, then a buy of 1 at 105, which the
  old code books as a partial cover of that short lot (a lot with closures).

Lot IDs are fixed (uuid4 is replaced), so a rebuild is byte-comparable apart
from SQLite page layout.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


async def _generate(out: Path, *, partial_cover: bool) -> None:
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from itertools import count
    from uuid import UUID

    from tradepulse.alerts import TelegramAlerter
    from tradepulse.models import AssetClass, AssetIdentity, ExecutionMode, Fill, SettlementEvent, Side, TradeIntent
    from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
    from tradepulse.settlement import SettlementProcessor, engine

    ids = count(1)
    engine.uuid4 = lambda: UUID(int=next(ids))  # type: ignore[attr-defined]
    t0 = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    out.unlink(missing_ok=True)
    database = AsyncSQLiteDatabase(f"sqlite:///{out}")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    processor = SettlementProcessor(repositories, TelegramAlerter(bot_token=None, chat_id=None), clock=lambda: t0 + timedelta(hours=1))
    steps = [("1", Side.BUY, "10", "100"), ("2", Side.SELL, "12", "110")] + ([("3", Side.BUY, "1", "105")] if partial_cover else [])
    for n, side, quantity, price in steps:
        at = t0 + timedelta(minutes=int(n))
        intent = TradeIntent(f"ti-{n}", f"idem-{n}", f"corr-{n}", asset, side, ExecutionMode.PAPER, "legacy_fixture", at,
                             requested_quantity=Decimal(quantity))
        await repositories.trade_intents.create_once(intent.trade_intent_id, intent, status=intent.status.value,
                                                     unique_value=intent.idempotency_key)
        fill = Fill(f"fill-{n}", f"ti-{n}", f"ord-{n}", asset, side, ExecutionMode.PAPER, Decimal(quantity), Decimal(price),
                    Decimal(0), Decimal(0), at, broker_fill_id=f"act-{n}")
        await repositories.fills.create_once(fill.fill_id, fill, unique_value=fill.broker_fill_id)
        event = SettlementEvent(f"se-{n}", fill.fill_id, f"ti-{n}", asset, side, ExecutionMode.PAPER, Decimal(quantity),
                                Decimal(price), at, broker_order_id=f"ord-{n}", broker_fill_id=f"act-{n}")
        await repositories.settlements.create_once(event.settlement_event_id, event, status=event.status.value,
                                                   unique_value=fill.fill_id)
        summary = await processor.process_pending()
        if not summary.ok:
            raise SystemExit(f"old settlement failed at step {n}: {summary}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="c10bb2fa")
    parser.add_argument("--generate", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.generate is not None:  # inside the old code's interpreter
        import tradepulse

        if not Path(tradepulse.__file__).resolve().is_relative_to(Path.cwd().resolve()):
            raise SystemExit(f"expected the exported old code, imported {tradepulse.__file__}")
        asyncio.run(_generate(args.generate / "legacy_short_open.db", partial_cover=False))
        asyncio.run(_generate(args.generate / "legacy_short_closed.db", partial_cover=True))
        return 0
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "old.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", "-o", str(archive), args.commit, "tradepulse"], check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(scratch, filter="data")
        env = {**os.environ, "PYTHONPATH": scratch}
        subprocess.run([sys.executable, "-P", __file__, "--generate", str(HERE)], check=True, env=env, cwd=scratch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
