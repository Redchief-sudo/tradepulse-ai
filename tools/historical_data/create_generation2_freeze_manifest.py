"""Creates the immutable Generation-2 calibration data-freeze manifest.

Establishes the authoritative contamination boundary proven by the
Rev.86-89 data-lineage audit (see docs/generation-2-calibration-freeze.md
for the full report): every historical bar currently available from
Alpaca has already been used or explicitly reported on by some Rev.86-89
calibration/forensic tool --

  - calibrate_exit_params.py's walk-forward folds score ALL history up to
    each fold's train_end as that fold's "train" window (fold_1's
    train_end=2022-12-31 alone already covers the entire pre-2023 cache);
  - diagnose_signal_sparsity.py and entry_composite_audit.py iterate the
    entire cached range with no date filter at all;
  - the regime-classifier Phase 1 report individually names and tabulates
    specific historical periods spanning the full cached range, including
    its earliest dates (e.g. "Dec 2018 low -> recovery", "COVID crash
    2020-02-19..2020-03-20").

There is no untouched historical window left. Only bars whose OWN market
date is strictly after the freeze boundary below are eligible for the
Generation-2 holdout reserve -- fetch/file timestamp is never the
authority; refetching an old bar after the freeze date does not make it
eligible.

This script produces DATA ONLY. It never selects, tunes, or evaluates a
calibration candidate, and it never inspects reserve-side (post-freeze)
data for anything -- it only records what was already contaminated as of
the freeze date, from data already on disk before this script runs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
CACHE_ROOT = REPO_ROOT / "data" / "calibration"
MANIFEST_PATH = CACHE_ROOT / "generation_2_freeze_manifest.json"

# The boundary established by the lineage audit: everything already
# cached/reported through this date is contaminated for Generation-2
# model-selection purposes. Changing this value is itself a scientific
# decision -- it must never be adjusted to make a later reserve look
# larger or more convenient; a new freeze requires a new, equally-audited
# justification.
FREEZE_DATE = date(2026, 9, 4)
MANIFEST_VERSION = 1


def _git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()


def _per_symbol_last_contaminated_date() -> dict[str, dict[str, str | None]]:
    per_symbol: dict[str, dict[str, str | None]] = {}
    for asset_class in ("equity", "crypto"):
        for path in sorted((CACHE_ROOT / asset_class).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            symbol = payload.get("symbol", path.stem)
            per_symbol[f"{asset_class}:{symbol}"] = {
                "asset_class": asset_class,
                "first_bar_date": payload.get("first_bar_date"),
                "last_bar_date": payload.get("last_bar_date"),
            }
    return per_symbol


def main() -> None:
    per_symbol = _per_symbol_last_contaminated_date()
    for key, info in per_symbol.items():
        last = info["last_bar_date"]
        if last is not None and date.fromisoformat(last) > FREEZE_DATE:
            print(
                f"WARNING: {key} last_bar_date {last} is AFTER the freeze date {FREEZE_DATE.isoformat()} -- "
                "this manifest must record data already contaminated as of the freeze, never future/reserve "
                "data. Re-check FREEZE_DATE before proceeding.",
                file=sys.stderr,
            )

    manifest_body = {
        "calibration_generation": 2,
        "manifest_version": MANIFEST_VERSION,
        "freeze_date": FREEZE_DATE.isoformat(),
        "eligibility_rule": f"bar_market_date > {FREEZE_DATE.isoformat()}",
        "source": "alpaca_iex_equity_and_alpaca_crypto",
        "purpose": "untouched_generation_2_holdout_reserve",
        "historical_data_through_freeze": "contaminated_for_model_selection",
        "contamination_basis": (
            "Rev.86-89 calibration/forensic tooling and reports have already used or explicitly reported on "
            "every date in the currently-available historical range for both equity and crypto -- see "
            "docs/generation-2-calibration-freeze.md for the full lineage audit. calibrate_exit_params.py's "
            "walk-forward folds score all history up to each fold's train_end as a 'train' window; "
            "diagnose_signal_sparsity.py and entry_composite_audit.py iterate the entire cached range with no "
            "date filter; the regime-classifier Phase 1 report individually names and tabulates specific "
            "historical periods spanning the full cached range, including its earliest dates."
        ),
        "rules": [
            "Every historical bar with market date <= freeze_date is contaminated for future "
            "model-selection/holdout purposes.",
            "Only bars whose actual market date is strictly greater than freeze_date may enter the "
            "Generation-2 untouched reserve.",
            "Refetching an older bar after the freeze date does NOT make it eligible -- eligibility is "
            "decided by the bar's own market date, never by fetch/file timestamp.",
            "The reserve may be accumulated (fetched and cached) but must NOT be loaded into "
            "calibration-selection, threshold-search, weight-search, diagnostics, or reports until the "
            "Generation-2 candidate is completely frozen.",
            "This manifest never decides which model wins -- it only proves what data was eligible before "
            "Generation-2 calibration starts.",
        ],
        "created_from_commit": _git_commit(),
        "per_symbol_last_contaminated_date": per_symbol,
    }

    # Hashed over the content fields only -- created_at is a wall-clock
    # audit-log timestamp, not a content fact, and must NOT make the
    # fingerprint change on every re-run when nothing substantive (freeze
    # date, commit, per-symbol contamination dates, rules) actually
    # changed. A genuinely deterministic fingerprint is the whole point:
    # regenerating this manifest against the same commit/cache state must
    # reproduce the identical hash.
    canonical = json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    integrity_sha256 = hashlib.sha256(canonical).hexdigest()

    manifest = {**manifest_body, "created_at": datetime.now(UTC).isoformat(), "integrity_sha256": integrity_sha256}
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {MANIFEST_PATH}")
    print(f"integrity_sha256={integrity_sha256}")


if __name__ == "__main__":
    main()
