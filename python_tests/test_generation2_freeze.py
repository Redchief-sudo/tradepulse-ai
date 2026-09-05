"""Validation gate for tools/historical_data/generation2_freeze.py -- the
shared guard any future Generation-2 calibration script must use to
classify bars, so the freeze boundary can only be changed in one place
(the manifest) and never re-derived or hardcoded per-script."""

import json
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "historical_data"
sys.path.insert(0, str(TOOL_DIR))

import generation2_freeze  # noqa: E402


def _write_manifest(monkeypatch, tmp_path: Path, freeze_date: str = "2026-09-04") -> None:
    manifest_path = tmp_path / "generation_2_freeze_manifest.json"
    manifest_path.write_text(json.dumps({"freeze_date": freeze_date}), encoding="utf-8")
    monkeypatch.setattr(generation2_freeze, "MANIFEST_PATH", manifest_path)


def test_freeze_date_is_read_from_the_manifest_not_hardcoded(monkeypatch, tmp_path) -> None:
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    assert generation2_freeze.freeze_date().isoformat() == "2026-09-04"


def test_missing_manifest_fails_closed_rather_than_guessing_a_boundary(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(generation2_freeze, "MANIFEST_PATH", tmp_path / "does-not-exist.json")
    with pytest.raises(generation2_freeze.Generation2FreezeError, match="does not exist"):
        generation2_freeze.freeze_date()


def test_malformed_manifest_json_fails_closed_as_generation2_freeze_error(monkeypatch, tmp_path) -> None:
    """Malformed JSON must still fail closed (never guess/default a
    boundary), but normalized to the module's own Generation2FreezeError
    rather than a raw JSONDecodeError leaking out -- with the original
    exception preserved via `raise ... from exc` for diagnosis."""
    manifest_path = tmp_path / "generation_2_freeze_manifest.json"
    manifest_path.write_text("{not valid json!!", encoding="utf-8")
    monkeypatch.setattr(generation2_freeze, "MANIFEST_PATH", manifest_path)

    with pytest.raises(generation2_freeze.Generation2FreezeError, match="malformed") as exc_info:
        generation2_freeze.freeze_date()
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_manifest_missing_freeze_date_field_fails_closed_as_generation2_freeze_error(monkeypatch, tmp_path) -> None:
    """Syntactically-valid JSON that is schema-corrupt (the required
    freeze_date field is simply absent) must fail closed the same way as
    malformed JSON -- never silently proceed with no boundary at all."""
    manifest_path = tmp_path / "generation_2_freeze_manifest.json"
    manifest_path.write_text(json.dumps({"manifest_version": 1}), encoding="utf-8")
    monkeypatch.setattr(generation2_freeze, "MANIFEST_PATH", manifest_path)

    with pytest.raises(generation2_freeze.Generation2FreezeError, match="missing the required 'freeze_date'") as exc_info:
        generation2_freeze.freeze_date()
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_manifest_invalid_freeze_date_value_fails_closed_as_generation2_freeze_error(monkeypatch, tmp_path) -> None:
    """A present but non-ISO-date freeze_date value must also fail closed
    as Generation2FreezeError, not a raw ValueError from date.fromisoformat."""
    manifest_path = tmp_path / "generation_2_freeze_manifest.json"
    manifest_path.write_text(json.dumps({"freeze_date": "not-a-date"}), encoding="utf-8")
    monkeypatch.setattr(generation2_freeze, "MANIFEST_PATH", manifest_path)

    with pytest.raises(generation2_freeze.Generation2FreezeError, match="not a valid ISO date") as exc_info:
        generation2_freeze.freeze_date()
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert not isinstance(exc_info.value.__cause__, generation2_freeze.Generation2FreezeError)


def test_a_bar_dated_exactly_on_the_freeze_date_is_not_eligible(monkeypatch, tmp_path) -> None:
    """The boundary is exclusive -- freeze_date itself is the last
    contaminated day, never the first reserve day."""
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    assert generation2_freeze.is_generation2_reserve_eligible("2026-09-04") is False


def test_a_bar_dated_the_day_after_the_freeze_date_is_eligible(monkeypatch, tmp_path) -> None:
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    assert generation2_freeze.is_generation2_reserve_eligible("2026-09-05") is True


def test_a_bar_dated_before_the_freeze_date_is_not_eligible(monkeypatch, tmp_path) -> None:
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    assert generation2_freeze.is_generation2_reserve_eligible("2020-07-27") is False


def test_assert_reserve_is_uncontaminated_passes_a_genuinely_clean_reserve(monkeypatch, tmp_path) -> None:
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    generation2_freeze.assert_reserve_is_uncontaminated(["2026-09-05", "2026-09-06", "2026-09-08"])


def test_assert_reserve_is_uncontaminated_raises_on_a_single_contaminated_date(monkeypatch, tmp_path) -> None:
    """A refetched/old bar sneaking into a claimed reserve must be caught
    immediately, not silently averaged away among otherwise-clean dates --
    mirrors the exact scenario the user flagged: refetching an older bar
    does not make it eligible."""
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    with pytest.raises(generation2_freeze.Generation2FreezeError, match="2020-07-27"):
        generation2_freeze.assert_reserve_is_uncontaminated(["2026-09-05", "2020-07-27", "2026-09-06"])


def test_assert_reserve_is_uncontaminated_raises_on_the_freeze_date_itself(monkeypatch, tmp_path) -> None:
    _write_manifest(monkeypatch, tmp_path, freeze_date="2026-09-04")
    with pytest.raises(generation2_freeze.Generation2FreezeError, match="2026-09-04"):
        generation2_freeze.assert_reserve_is_uncontaminated(["2026-09-05", "2026-09-04"])
