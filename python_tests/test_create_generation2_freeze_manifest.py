"""Validation gate for tools/historical_data/create_generation2_freeze_manifest.py.

The one property that actually matters here: the integrity fingerprint
must be a genuine content fingerprint, reproducible across re-runs against
the same underlying data -- not an accident of wall-clock timing. This was
a real bug caught while building this tool (created_at was originally
hashed alongside the content fields, making every re-run produce a
different hash for identical data)."""

import json
import sys
import time
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

import create_generation2_freeze_manifest as creator  # noqa: E402


def _seed_cache(cache_root: Path) -> None:
    (cache_root / "equity").mkdir(parents=True, exist_ok=True)
    (cache_root / "crypto").mkdir(parents=True, exist_ok=True)
    (cache_root / "equity" / "AAPL.json").write_text(
        json.dumps({"symbol": "AAPL", "asset_class": "equity", "first_bar_date": "2020-07-27", "last_bar_date": "2026-09-03"}),
        encoding="utf-8",
    )
    (cache_root / "crypto" / "BTC-USD.json").write_text(
        json.dumps({"symbol": "BTC/USD", "asset_class": "crypto", "first_bar_date": "2021-01-01", "last_bar_date": "2026-09-04"}),
        encoding="utf-8",
    )


def _configure(monkeypatch, cache_root: Path) -> Path:
    manifest_path = cache_root / "generation_2_freeze_manifest.json"
    monkeypatch.setattr(creator, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(creator, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(creator, "_git_commit", lambda: "deadbeef")
    return manifest_path


def _run(monkeypatch, tmp_path: Path) -> dict:
    """Seeds a fresh cache and runs the creator once -- only for tests
    that don't need to change cache content between runs."""
    cache_root = tmp_path / "calibration"
    _seed_cache(cache_root)
    manifest_path = _configure(monkeypatch, cache_root)
    creator.main()
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def test_integrity_hash_is_deterministic_across_reruns_against_identical_data(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "calibration"
    _seed_cache(cache_root)
    manifest_path = _configure(monkeypatch, cache_root)

    creator.main()
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    time.sleep(1.1)  # forces a different wall-clock second for created_at
    creator.main()  # reruns against the SAME on-disk cache content, unmodified
    second = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert first["created_at"] != second["created_at"]  # sanity: the clock really did advance
    assert first["integrity_sha256"] == second["integrity_sha256"]


def test_integrity_hash_changes_when_underlying_data_changes(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "calibration"
    _seed_cache(cache_root)
    manifest_path = _configure(monkeypatch, cache_root)

    creator.main()
    first = json.loads(manifest_path.read_text(encoding="utf-8"))

    (cache_root / "equity" / "AAPL.json").write_text(
        json.dumps({"symbol": "AAPL", "asset_class": "equity", "first_bar_date": "2020-07-27", "last_bar_date": "2026-09-04"}),
        encoding="utf-8",
    )
    creator.main()
    second = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert first["integrity_sha256"] != second["integrity_sha256"]


def test_manifest_records_per_symbol_contamination_dates_and_commit(monkeypatch, tmp_path) -> None:
    manifest = _run(monkeypatch, tmp_path)
    assert manifest["created_from_commit"] == "deadbeef"
    assert manifest["freeze_date"] == "2026-09-04"
    assert manifest["per_symbol_last_contaminated_date"]["equity:AAPL"]["last_bar_date"] == "2026-09-03"
    assert manifest["per_symbol_last_contaminated_date"]["crypto:BTC/USD"]["last_bar_date"] == "2026-09-04"


def test_warns_but_still_writes_when_a_cached_bar_is_after_the_freeze_date(monkeypatch, tmp_path, capsys) -> None:
    cache_root = tmp_path / "calibration"
    _seed_cache(cache_root)
    (cache_root / "equity" / "AAPL.json").write_text(
        json.dumps({"symbol": "AAPL", "asset_class": "equity", "first_bar_date": "2020-07-27", "last_bar_date": "2026-09-10"}),
        encoding="utf-8",
    )
    manifest_path = _configure(monkeypatch, cache_root)
    creator.main()
    assert "WARNING" in capsys.readouterr().err
    assert manifest_path.exists()
