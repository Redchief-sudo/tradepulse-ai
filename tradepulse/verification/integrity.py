"""Deterministic source manifests and create-once generation artifacts.

All functions here perform blocking filesystem I/O. Async callers MUST use
asyncio.to_thread. This is an integrity detector, not a hostile-host sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "paper-verification-v1"
SOURCE_SUFFIXES = frozenset({".py", ".json", ".toml", ".yaml", ".yml", ".sql", ".ini", ".cfg"})
ROOT_AUTHORITY = ("pyproject.toml", "uv.lock", "requirements.txt", "requirements.lock")
GENERATION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class VerificationError(ValueError):
    """Official evidence cannot be certified; never an instruction to trade."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def generation_path(store: Path, generation: str) -> Path:
    if not GENERATION_PATTERN.fullmatch(generation):
        raise VerificationError("invalid_generation_id")
    return store / generation


def source_files(root: Path, *, require_source: bool = True) -> dict[str, str]:
    """Protect package source/static authority, not runtime artifacts.

    All Python and supported static configuration/schema files under the
    package are protected, including newly added files. Symlinks are refused
    so authority cannot escape enumeration. Caches contain no source authority.
    """
    package = root / "tradepulse"
    if package.is_symlink() or (require_source and not package.is_dir()):
        raise VerificationError("production_source_missing")
    paths: list[Path] = []
    def enumeration_error(error):
        raise error

    walk = os.walk(package, followlinks=False, onerror=enumeration_error) if package.exists() else ()
    for directory, dirs, files in walk:
        parent = Path(directory)
        for name in dirs:
            if (parent / name).is_symlink():
                raise VerificationError(f"source_symlink:{(parent / name).relative_to(root).as_posix()}")
        dirs[:] = sorted(d for d in dirs if d not in {"__pycache__", ".pytest_cache"})
        for name in files:
            path = parent / name
            if path.suffix.lower() in SOURCE_SUFFIXES:
                paths.append(path)
    paths.extend(root / name for name in ROOT_AUTHORITY if (root / name).exists())
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"source_not_regular:{path.relative_to(root).as_posix()}")
        result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    if require_source and not any(name.endswith(".py") for name in result):
        raise VerificationError("production_source_empty")
    return result


def write_once(path: Path, value: Any) -> None:
    """Publish complete, fsynced bytes atomically without replacing a prior artifact."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # atomic no-replace publication
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise VerificationError("duplicate_json_key")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise VerificationError("invalid_artifact_object")
    return value


def freeze_source(root: Path, directory: Path, generation: str, *, context: dict, policy: dict, revision: str) -> dict:
    if directory.exists():
        raise VerificationError("generation_already_exists")
    files = source_files(root)
    manifest = {
        "schema": SCHEMA, "verification_generation_id": generation,
        "created_at": utc_now(), "mode": "paper", "source_revision": revision,
        "protected_file_count": len(files), "protected_files": files,
        "aggregate_sha256": digest(canonical(files)), "verification_status": "FROZEN",
        "context": context, "policy": policy,
    }
    directory.mkdir(parents=True, exist_ok=False)
    write_once(directory / "manifest.json", manifest)
    write_once(directory / "manifest-sha256.json", {"sha256": digest(canonical(manifest))})
    return manifest


def read_manifest(directory: Path) -> dict:
    manifest = load_json(directory / "manifest.json")
    expected = load_json(directory / "manifest-sha256.json")
    if expected != {"sha256": digest(canonical(manifest))}:
        raise VerificationError("manifest_digest_mismatch")
    required = {"schema", "verification_generation_id", "created_at", "mode", "source_revision",
                "protected_file_count", "protected_files", "aggregate_sha256", "verification_status", "context", "policy"}
    if set(manifest) != required or manifest["schema"] != SCHEMA or manifest["mode"] != "paper":
        raise VerificationError("manifest_schema_invalid")
    if manifest["verification_generation_id"] != directory.name or manifest["verification_status"] != "FROZEN":
        raise VerificationError("manifest_generation_invalid")
    stamp = datetime.fromisoformat(manifest["created_at"])
    if stamp.tzinfo is None or stamp > datetime.now(UTC):
        raise VerificationError("manifest_timestamp_invalid")
    files = manifest["protected_files"]
    if not isinstance(files, dict) or not files or manifest["protected_file_count"] != len(files):
        raise VerificationError("manifest_file_count_invalid")
    for name, value in files.items():
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
            raise VerificationError("manifest_path_invalid")
        if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
            raise VerificationError("manifest_file_digest_invalid")
    if manifest["aggregate_sha256"] != digest(canonical(files)):
        raise VerificationError("manifest_aggregate_invalid")
    if not isinstance(manifest["context"], dict) or not isinstance(manifest["policy"], dict):
        raise VerificationError("manifest_context_invalid")
    return manifest


def verify_source(root: Path, directory: Path) -> dict:
    manifest = read_manifest(directory)
    frozen = manifest["protected_files"]
    current = source_files(root, require_source=False)
    modified = sorted(name for name in frozen.keys() & current.keys() if frozen[name] != current[name])
    missing = sorted(frozen.keys() - current.keys())
    added = sorted(current.keys() - frozen.keys())
    return {
        "generation": manifest["verification_generation_id"], "frozen": True,
        "integrity_valid": not (modified or missing or added),
        "aggregate_sha256": manifest["aggregate_sha256"], "current_aggregate_sha256": digest(canonical(current)),
        "protected_file_count": len(frozen), "modified": modified, "missing": missing, "added": added,
    }
