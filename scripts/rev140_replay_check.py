"""Replay a preserved database under Rev.140, on a copy only (spec test 21).

    python scripts/rev140_replay_check.py --copy-from <database> --report <report.json>
    python scripts/rev140_replay_check.py --database <disposable.db> --report <report.json>

``--copy-from`` takes a consistent copy with SQLite's online backup API,
reading the source read-only, into a fresh 0700 scratch directory, and
works on that copy. ``--database`` works on a file in place, so it refuses
any path under ``data/soaks/`` and any file that has ever held financial
evidence (``ever_evidence = 1``).

On the copy it opens the database with Rev.140's schema, then runs
``accounting_issues``, the accounting replay, ``accounting_issues`` again,
``check_c1`` and the open-short-lot scan, and records each table's digest
before and after the replay. The source's SHA-256 is recorded before and
after, and must not change. Exit status 0 means: no issues, no ledger
changes, no legacy short lot, and an unchanged source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

LEDGERS = ("fills", "settlements", "position_lots", "holdings", "cash_ledger", "pnl_records", "trade_attributions",
           "short_exposure_liabilities", "quarantine_executions", "legacy_lot_conversions")


class ReplayRefused(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _under_soaks(path: Path) -> bool:
    parts = path.resolve().parts
    return any(parts[i:i + 2] == ("data", "soaks") for i in range(len(parts) - 1))


def _ever_evidence(path: Path) -> bool:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT ever_evidence FROM verification_identity WHERE singleton=1").fetchone()
    except sqlite3.OperationalError:
        return False  # predates the identity table
    finally:
        connection.close()
    return bool(row and row[0])


def refuse_in_place(path: Path) -> None:
    if _under_soaks(path):
        raise ReplayRefused("REPLAY_REFUSED_PRESERVED_SOAK_PATH: use --copy-from")
    if _ever_evidence(path):
        raise ReplayRefused("REPLAY_REFUSED_EVIDENCE_DATABASE: use --copy-from")


def backup_copy(source: Path, directory: Path) -> Path:
    target = directory / (source.name + ".rev140-replay")
    reader = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    return target


def table_digests(path: Path) -> dict[str, str | None]:
    connection = sqlite3.connect(path)
    try:
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: sha256(json.dumps(connection.execute(f"SELECT * FROM {table} ORDER BY record_id").fetchall(),
                                         default=str).encode()).hexdigest() if table in present else None
                for table in LEDGERS}
    finally:
        connection.close()


async def check(path: Path) -> dict[str, Any]:
    from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
    from tradepulse.quarantine.fees import check_c1
    from tradepulse.quarantine.legacy import find_open_short_lots
    from tradepulse.settlement.accounting import accounting_issues, replay_accounting

    database = AsyncSQLiteDatabase(f"sqlite:///{path}")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    before = table_digests(path)
    issues_before = await accounting_issues(repositories)
    try:
        changed: int | str = await replay_accounting(repositories)
    except Exception as exc:  # noqa: BLE001 - reported, never raised past the report
        changed = f"REPLAY_FAILED: {exc}"
    after = table_digests(path)
    issues = await accounting_issues(repositories)
    c1 = await check_c1(repositories)
    shorts = [lot.lot_id for lot in await find_open_short_lots(repositories)]
    if c1:
        issues["C1"] = ", ".join(c1)
    if shorts:
        issues["UNSUPPORTED_SHORT_LOT"] = ", ".join(shorts)
    if isinstance(changed, str):
        issues["replay"] = changed
    return {"issues_before_replay": issues_before, "issues": issues, "inserted_rows": changed,
            "digests_before": before, "digests_after": after, "ledgers_identical": before == after}


def run(source: Path | None, database: Path | None, report: Path) -> int:
    if (source is None) == (database is None):
        raise ReplayRefused("exactly one of --copy-from and --database is required")
    original = source if source is not None else database
    assert original is not None
    if not original.is_file():
        raise ReplayRefused(f"no such database: {original}")
    sha_before = file_sha256(original)
    with tempfile.TemporaryDirectory(prefix="rev140-replay-") as scratch:
        os.chmod(scratch, 0o700)
        if source is not None:
            target = backup_copy(source, Path(scratch))
        else:
            refuse_in_place(original)
            target = original
        result = asyncio.run(check(target))
    sha_after = file_sha256(original)
    payload = {"source": str(original), "mode": "copy" if source is not None else "in_place",
               "source_sha256_before": sha_before, "source_sha256_after": sha_after, **result}
    report.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    ok = (not result["issues"] and result["ledgers_identical"] and result["inserted_rows"] == 0
          and (source is None or sha_before == sha_after))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copy-from", type=Path, help="the database to copy, read-only, and replay")
    parser.add_argument("--database", type=Path, help="a disposable database to replay in place")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.copy_from, args.database, args.report)
    except ReplayRefused as exc:
        print(f"rev140_replay_check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
