"""Generation lifecycle and async runtime guard; no financial writes."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

from tradepulse.config import Settings
from tradepulse.persistence import AsyncSQLiteDatabase
from tradepulse.provenance import get_provenance

from .evidence import VerificationPolicy, assess, snapshot_database, timestamp
from .integrity import (
    VerificationError, canonical, digest, freeze_source, generation_path, load_json,
    read_manifest, utc_now, verify_source, write_once,
)

logger = logging.getLogger(__name__)


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def store_for(settings: Settings) -> Path:
    database = AsyncSQLiteDatabase(settings.database_url).path
    return database.parent / (database.name + ".paper-verification")


def context_for(settings: Settings) -> dict:
    # Secrets are never serialized. Account credentials are bound by a digest
    # so replacing them cannot silently route one generation to another account.
    secret_fields = {field.name for field in fields(settings) if any(part in field.name for part in ("key", "secret", "token", "chat_id"))}
    context = {field.name: getattr(settings, field.name) for field in fields(settings)
               if field.name not in secret_fields and field.name not in {"log_level", "database_url"}}
    context["database_path"] = str(AsyncSQLiteDatabase(settings.database_url).path)
    context["broker_credentials_sha256"] = digest(canonical([settings.alpaca_api_key, settings.alpaca_api_secret]))
    for name in ("equity_universe_path", "crypto_universe_path", "options_universe_path"):
        path = getattr(settings, name)
        context[name] = {"path": str(Path(path).resolve()), "sha256": digest(Path(path).read_bytes())} if path else None
    return context


def freeze(settings: Settings, generation: str, costs: dict | None, root: Path | None = None) -> dict:
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise VerificationError("verification_requires_paper_with_live_disabled")
    store = store_for(settings)
    directory = generation_path(store, generation)
    rows = snapshot_database(AsyncSQLiteDatabase(settings.database_url).path)
    # Dedicated fresh database: no timestamp-only guessing of dataset ownership.
    if any(rows[table] for table in rows if table not in {"trading_sessions", "audit_events"}):
        raise VerificationError("freeze_requires_fresh_evidence_database")
    if (store / "binding.json").exists():
        raise VerificationError("database_already_bound_use_new_database_for_new_generation")
    root = root or source_root()
    manifest = freeze_source(root, directory, generation, context=context_for(settings),
                             policy={"thresholds": VerificationPolicy().as_dict(), "costs": costs},
                             revision=get_provenance(repo_path=root).git_commit)
    write_once(store / "binding.json", {"generation": generation, "manifest_sha256": digest(canonical(manifest))})
    return manifest


def bound_generation(settings: Settings) -> str | None:
    path = store_for(settings) / "binding.json"
    if not path.exists():
        if path.parent.exists():
            raise VerificationError("generation_binding_missing")
        return None
    return load_json(path)["generation"]


class Verification:
    def __init__(self, settings: Settings, generation: str, *, root: Path | None = None):
        self.settings = settings
        self.generation = generation
        self.root = root or source_root()
        self.directory = generation_path(store_for(settings), generation)
        self.database = AsyncSQLiteDatabase(settings.database_url).path
        self._lease = None

    def check(self) -> dict:
        try:
            manifest = read_manifest(self.directory)
            binding = load_json(store_for(self.settings) / "binding.json")
            if binding != {"generation": self.generation, "manifest_sha256": digest(canonical(manifest))}:
                raise VerificationError("generation_binding_mismatch")
            result = verify_source(self.root, self.directory)
            if (self.settings.execution_mode != "paper" or self.settings.live_trading_enabled
                    or manifest["context"] != context_for(self.settings)):
                raise VerificationError("frozen_configuration_mismatch")
            if manifest["policy"]["thresholds"] != VerificationPolicy().as_dict():
                raise VerificationError("frozen_threshold_mismatch")
            if not result["integrity_valid"] and not (self.directory / "seal.json").exists():
                self.invalidate(result)
            if (self.directory / "invalid.json").exists():
                result["integrity_valid"] = False
                result["failure"] = load_json(self.directory / "invalid.json")
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failure = {"generation": self.generation, "frozen": (self.directory / "manifest.json").exists(),
                       "integrity_valid": False, "error": type(exc).__name__, "reason": str(exc)}
            if self.directory.is_dir():
                self.invalidate(failure)
            return failure

    def invalidate(self, reason: dict) -> None:
        try:
            write_once(self.directory / "invalid.json", {"at": utc_now(), "reason": reason})
        except FileExistsError:
            pass
        # Other I/O failures propagate to the caller and still refuse startup.

    def started_at(self) -> str:
        started = load_json(self.directory / "started.json")
        if load_json(self.directory / "started-sha256.json") != {"sha256": digest(canonical(started))}:
            raise VerificationError("start_record_digest_mismatch")
        stamp = timestamp(started["started_at"])
        if not timestamp(read_manifest(self.directory)["created_at"]) <= stamp <= datetime.now(UTC):
            raise VerificationError("start_record_timestamp_invalid")
        return started["started_at"]

    def begin(self) -> None:
        if (self.directory / "started.json").exists():
            self.started_at()
            return
        started = {"started_at": utc_now()}
        write_once(self.directory / "started.json", started)
        write_once(self.directory / "started-sha256.json", {"sha256": digest(canonical(started))})

    def report(self, *, seal: bool = False) -> dict:
        integrity = self.check()
        if (self.directory / "seal.json").exists():
            sealed = load_json(self.directory / "seal.json")
            evidence = load_json(self.directory / "sealed-evidence.json")
            if (load_json(self.directory / "seal-sha256.json") != {"sha256": digest(canonical(sealed))}
                    or sealed["evidence_sha256"] != digest(canonical(evidence))
                    or sealed["manifest_sha256"] != digest(canonical(read_manifest(self.directory)))):
                raise VerificationError("sealed_evidence_digest_mismatch")
            return {**sealed, "integrity": integrity}
        if not integrity["integrity_valid"]:
            return {"status": "PROVE_EDGE_FAILED_INTEGRITY", "integrity": integrity}
        if (self.directory / "sealed-evidence.json").exists():
            raise VerificationError("incomplete_seal_requires_audit")
        if not (self.directory / "started.json").exists():
            return {"status": "PROVE_EDGE_IN_PROGRESS", "reason": "generation_not_started", "integrity": integrity}
        started = self.started_at()
        manifest = read_manifest(self.directory)
        rows = snapshot_database(self.database)
        result = assess(rows, started, datetime.now(UTC), manifest["policy"]["costs"])
        if seal and result["status"] == "PROVE_EDGE_PASSED":
            # Only the process holding the generation lease may seal, after
            # all supervised work has drained (or from an idle CLI assessment).
            if self._lease is None:
                raise VerificationError("seal_requires_exclusive_generation_lease")
            if not self.check()["integrity_valid"]:
                raise VerificationError("source_changed_before_seal")
            result.update({"workflow": "POST_PROVE_EDGE_DEVELOPMENT", "generation": self.generation,
                           "sealed_at": utc_now(), "manifest_sha256": digest(canonical(manifest)),
                           "source_sha256": manifest["aggregate_sha256"]})
            # A crash between these publications fails closed, never overwrites.
            write_once(self.directory / "sealed-evidence.json", rows)
            write_once(self.directory / "seal.json", result)
            write_once(self.directory / "seal-sha256.json", {"sha256": digest(canonical(result))})
        return {**result, "integrity": integrity}

    def acquire(self) -> None:
        import fcntl

        if self._lease is not None:
            raise VerificationError("generation_lease_already_held")
        if not self.check()["integrity_valid"]:
            raise VerificationError("official_verification_integrity_invalid")
        stream = (self.directory / "process.lock").open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise VerificationError("generation_process_already_active") from None
        self._lease = stream

    def release(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    async def start(self) -> bool:
        try:
            await asyncio.to_thread(self.acquire)
            if await asyncio.to_thread((self.directory / "seal.json").exists):
                raise VerificationError("generation_sealed_new_generation_required")
            if await asyncio.to_thread((self.directory / "sealed-evidence.json").exists):
                raise VerificationError("incomplete_seal_requires_audit")
            await asyncio.to_thread(self.begin)
            logger.info("paper_verification_started", extra={"event": "paper_verification_started", "generation": self.generation})
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            await asyncio.to_thread(self.release)
            logger.error("paper_verification_refused", extra={"event": "paper_verification_refused", "reason": str(exc)})
            return False

    async def watch(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            try:
                result = await asyncio.to_thread(self.report)
                if not result["integrity"]["integrity_valid"] or result["status"] == "PROVE_EDGE_PASSED":
                    logger.warning("paper_verification_stopping", extra={"event": "paper_verification_stopping", "result": result})
                    shutdown.set()
                    return
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error) as exc:
                logger.error("paper_verification_check_failed", extra={"event": "paper_verification_check_failed", "reason": str(exc)})
                shutdown.set()
                return
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=5)
            except TimeoutError:
                pass

    async def finish(self) -> dict:
        try:
            return await asyncio.to_thread(self.report, seal=True)
        finally:
            await asyncio.to_thread(self.release)
