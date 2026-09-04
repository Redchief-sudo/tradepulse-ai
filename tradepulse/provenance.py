"""TradePulse ownership and build-provenance metadata.

Pure, read-only information about WHO created this software and WHICH
revision is running -- creator/copyright identity, version, git commit, and
a deterministic build fingerprint derived from those. This module is never
consulted by any scanning, strategy, regime, sizing, risk, execution,
settlement, reconciliation, or session-state decision; it exists solely for
ownership/provenance display (CLI, dashboard, API, release manifest) and
carries no trading authority whatsoever.

The build fingerprint is NOT a secret, a DRM mechanism, a machine/device
tracker, or a substitute for git history -- it is a documented, reproducible
SHA-256 over a small set of PUBLIC identity/version fields. It never reads
credentials, `.env` values, or anything machine-identifying (no hostname,
IP, MAC, disk serial, or home-directory path).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path

PRODUCT_NAME = "TradePulse"
CREATOR_NAME = "Damien Johnson Fisher"
COPYRIGHT_OWNER = "Damien Johnson Fisher"
COMPANY_NAME = "Silvereyes Technologies, LLC"
COPYRIGHT_YEARS = "2025-2026"
PROVENANCE_VERSION = "1"

# Stable, hand-written slugs for the canonical fingerprint string -- kept
# literal (not derived from CREATOR_NAME/COMPANY_NAME) so a future display-
# text edit (e.g. punctuation) can never silently change what the
# fingerprint means.
_CREATOR_SLUG = "damien-johnson-fisher"
_COMPANY_SLUG = "silvereyes-technologies-llc"
_CANONICAL_TAG = "provenance-v1"

_PACKAGE_DISTRIBUTION_NAME = "tradepulse-runtime"


def _software_version() -> str:
    try:
        return _package_version(_PACKAGE_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "unknown"


def _git_commit(cwd: Path | None = None) -> str:
    """A read-only `git rev-parse HEAD` -- never raises, never blocks
    startup on missing git or a missing .git directory (e.g. a release
    archive with no VCS metadata at all). Falls back to the truthful
    "unknown" rather than fabricating a commit identifier."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    commit = result.stdout.strip()
    return commit or "unknown"


def _canonical_string(software_version: str, git_commit: str) -> str:
    """Documented, deterministic serialization -- the SAME inputs always
    produce the SAME string (and therefore the same fingerprint); changing
    an authoritative input (the git commit, in particular) always changes
    it. Contains only public identity/version fields -- no credentials, no
    machine-identifying data."""
    return f"tradepulse|{_CREATOR_SLUG}|{_COMPANY_SLUG}|{software_version}|{git_commit}|{_CANONICAL_TAG}"


def _build_fingerprint(software_version: str, git_commit: str) -> str:
    canonical = _canonical_string(software_version, git_commit)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Provenance:
    product_name: str
    creator_name: str
    copyright_owner: str
    company_name: str
    copyright_years: str
    software_version: str
    git_commit: str
    build_timestamp: str
    provenance_version: str
    build_fingerprint: str


def get_provenance(*, now: datetime | None = None, repo_path: Path | None = None) -> Provenance:
    """Builds the full provenance record. `now`/`repo_path` are injectable
    only for tests -- production callers always use the defaults (current
    UTC time, this module's own repo location)."""
    software_version = _software_version()
    git_commit = _git_commit(cwd=repo_path)
    return Provenance(
        product_name=PRODUCT_NAME,
        creator_name=CREATOR_NAME,
        copyright_owner=COPYRIGHT_OWNER,
        company_name=COMPANY_NAME,
        copyright_years=COPYRIGHT_YEARS,
        software_version=software_version,
        git_commit=git_commit,
        build_timestamp=(now or datetime.now(UTC)).isoformat(),
        provenance_version=PROVENANCE_VERSION,
        build_fingerprint=_build_fingerprint(software_version, git_commit),
    )
