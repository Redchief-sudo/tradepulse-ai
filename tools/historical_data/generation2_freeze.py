"""Shared guard for any future Generation-2 calibration tooling.

Enforces the immutable freeze boundary established in
data/calibration/generation_2_freeze_manifest.json (see
docs/generation-2-calibration-freeze.md for the full lineage audit that
justifies it). Any script that evaluates a Generation-2 holdout/reserve
candidate must classify bars through is_generation2_reserve_eligible() /
assert_reserve_is_uncontaminated() here, rather than re-deriving or
hardcoding the freeze_date itself -- that way the boundary can only ever
be changed in one place (the manifest), and any accidental mixing of
pre-freeze development data into the reserve is caught immediately rather
than silently producing a result that looks like a clean holdout but
isn't.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
MANIFEST_PATH = REPO_ROOT / "data" / "calibration" / "generation_2_freeze_manifest.json"


class Generation2FreezeError(ValueError):
    """Raised when calibration tooling attempts to treat contaminated
    (at-or-before-freeze) data as part of the Generation-2 untouched
    reserve, or when the manifest itself is missing."""


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise Generation2FreezeError(
            f"{MANIFEST_PATH} does not exist -- run create_generation2_freeze_manifest.py first. "
            "No Generation-2 tooling may guess or hardcode a freeze boundary."
        )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def freeze_date() -> date:
    return date.fromisoformat(_load_manifest()["freeze_date"])


def is_generation2_reserve_eligible(market_date: str) -> bool:
    """True only for a bar whose own market date is strictly after the
    freeze boundary -- never based on when the bar was fetched/cached."""
    return date.fromisoformat(market_date) > freeze_date()


def assert_reserve_is_uncontaminated(market_dates: list[str]) -> None:
    """Guard for a future Generation-2 script to call before treating a
    set of dates as the untouched reserve/holdout -- raises immediately if
    any contaminated (at-or-before-freeze) date has been included, rather
    than silently producing a result that looks like a clean holdout but
    isn't."""
    contaminated = sorted(d for d in market_dates if not is_generation2_reserve_eligible(d))
    if contaminated:
        boundary = freeze_date().isoformat()
        sample = ", ".join(contaminated[:5])
        raise Generation2FreezeError(
            f"{len(contaminated)} date(s) at or before the freeze boundary ({boundary}) were passed as part "
            f"of the Generation-2 reserve -- e.g. {sample}. Refetching an old bar does not make it eligible; "
            "only its own market date decides this."
        )


__all__ = [
    "Generation2FreezeError",
    "assert_reserve_is_uncontaminated",
    "freeze_date",
    "is_generation2_reserve_eligible",
]
