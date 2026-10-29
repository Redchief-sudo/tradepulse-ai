"""Rev.140 task 13: replay of preserved databases (spec test 21), on fixtures only."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "rev140"
_spec = importlib.util.spec_from_file_location("rev140_replay_check", ROOT / "scripts" / "rev140_replay_check.py")
assert _spec is not None and _spec.loader is not None
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def _report(path: Path) -> Any:
    return json.loads(path.read_text())


@pytest.mark.parametrize("name", ["legacy_short_open.db", "legacy_short_closed.db"])
def test_replay_fixture_identical(tmp_path: Path, name: str) -> None:
    """The legacy fixtures replay to identical digests, twice, from a copy;
    the source is untouched. Each fixture's open short lot is reported."""
    source = tmp_path / name
    shutil.copyfile(FIXTURES / name, source)
    reports = [tmp_path / "first.json", tmp_path / "second.json"]
    codes = [replay.main(["--copy-from", str(source), "--report", str(report)]) for report in reports]
    first, second = (_report(report) for report in reports)
    assert codes == [1, 1]  # the legacy short lot is an issue until adopted
    assert set(first["issues"]) == {"UNSUPPORTED_SHORT_LOT"}
    assert first["ledgers_identical"] and first["inserted_rows"] == 0
    assert first["digests_after"] == second["digests_after"]
    assert first["source_sha256_before"] == first["source_sha256_after"] == second["source_sha256_after"]


def test_a_clean_database_passes(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    assert replay.main(["--copy-from", str(database), "--report", str(tmp_path / "r.json")]) == 0
    assert _report(tmp_path / "r.json")["issues"] == {}


def test_refuses_evidence_path(tmp_path: Path) -> None:
    soaks = tmp_path / "data" / "soaks" / "run"
    soaks.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "legacy_short_open.db", soaks / "soak.db")
    before = (soaks / "soak.db").read_bytes()
    assert replay.main(["--database", str(soaks / "soak.db"), "--report", str(tmp_path / "r.json")]) == 2
    evidence = tmp_path / "evidence.db"
    shutil.copyfile(FIXTURES / "legacy_short_open.db", evidence)  # ever_evidence = 1
    assert replay.main(["--database", str(evidence), "--report", str(tmp_path / "r.json")]) == 2
    assert (soaks / "soak.db").read_bytes() == before
    assert not (tmp_path / "r.json").exists()
