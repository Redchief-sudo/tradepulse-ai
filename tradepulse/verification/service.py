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
    read_manifest, source_files, utc_now, verify_source, write_once,
)
from .opening import capture_checkpoint, database_identity, load_opening_checkpoint

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


def _fresh_identity(database: Path, generation: str) -> dict:
    if database.name.lower() == "tradepulse.db":
        raise VerificationError("official_database_must_be_new_dedicated_database")
    if not generation.startswith("soak-") and "soak" in database.name.lower():
        raise VerificationError("official_generation_cannot_use_soak_database")
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        return _require_fresh(connection)
    finally:
        connection.close()


def _require_fresh(connection) -> dict:
    identity = database_identity(connection)
    if identity["generation_id"] is not None:
        raise VerificationError("database_already_bound_use_new_database_for_new_generation")
    if identity["ever_evidence"] or identity["legacy_database"]:
        raise VerificationError("freeze_requires_fresh_evidence_database")
    # Check every application table, not just the performance-assessment subset.
    # Prior rows remain disqualifying even if their sticky marker was corrupted.
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table in {"verification_identity", "trading_sessions", "audit_events", "locks"} or table.startswith("sqlite_"):
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        if connection.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
            raise VerificationError("freeze_requires_fresh_evidence_database")
    return identity


def _publish_freeze(settings, generation, costs, root, identity, checkpoint, soak_proof):
    database = AsyncSQLiteDatabase(settings.database_url).path
    store = store_for(settings)
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _require_fresh(connection) != identity:
            raise VerificationError("database_changed_during_opening_capture")
        if store.exists():
            raise VerificationError("generation_artifacts_already_exist")
        manifest = freeze_source(root, generation_path(store, generation), generation,
                                 context=context_for(settings), opening_checkpoint=checkpoint,
                                 policy={"thresholds": VerificationPolicy().as_dict(), "costs": costs,
                                         "soak_prerequisites": soak_proof},
                                 revision=get_provenance(repo_path=root).git_commit)
        manifest_digest = digest(canonical(manifest))
        write_once(store / "binding.json", {"generation": generation, "manifest_sha256": manifest_digest,
                   "database_identity": identity["database_id"], "checkpoint_id": checkpoint["checkpoint_id"]})
        changed = connection.execute(
            "UPDATE verification_identity SET generation_id=?,checkpoint_id=?,manifest_digest=? "
            "WHERE singleton=1 AND generation_id IS NULL AND ever_evidence=0 AND legacy_database=0",
            (generation, checkpoint["checkpoint_id"], manifest_digest),
        )
        if changed.rowcount != 1:
            raise VerificationError("database_generation_bind_failed")
        connection.commit()
        return manifest
    finally:
        connection.close()


async def freeze(settings: Settings, generation: str, costs: dict | None, root: Path | None = None,
                 *, broker=None, soak_reports=()) -> dict:
    if settings.execution_mode != "paper" or settings.live_trading_enabled:
        raise VerificationError("verification_requires_paper_with_live_disabled")
    from .evidence import number
    if costs is None or {key: number(value) for key, value in costs.items()} != {
            "fee_bps": number("25"), "slippage_bps": number("15")}:
        raise VerificationError("authorized_verification_overlay_requires_fee_25_slippage_15_bps")
    costs = {"fee_bps": "25", "slippage_bps": "15"}
    database = await asyncio.to_thread(AsyncSQLiteDatabase, settings.database_url)
    store = await asyncio.to_thread(store_for, settings)
    generation_path(store, generation)
    if await asyncio.to_thread(store.exists):
        raise VerificationError("generation_artifacts_already_exist")
    identity = await asyncio.to_thread(_fresh_identity, database.path, generation)
    root = root or source_root()
    context = await asyncio.to_thread(context_for, settings)
    source_digest = digest(canonical(await asyncio.to_thread(source_files, root)))
    soak_proof = None
    if not generation.startswith("soak-"):
        from .soak import verify_soak_prerequisites
        soak_proof = await asyncio.to_thread(verify_soak_prerequisites, soak_reports,
                                             source_digest=source_digest, context=context, costs=costs)
        if identity["database_id"] in soak_proof.get("database_identities", []):
            raise VerificationError("official_generation_cannot_reuse_soak_database")
    owns_broker = broker is None
    if owns_broker:
        from tradepulse.session_commands import build_broker, require_credentials
        require_credentials(settings, require_ai=False)
        broker = build_broker(settings)
    try:
        checkpoint = await capture_checkpoint(broker, generation=generation, database_identity=identity["database_id"],
                                               source_manifest_digest=source_digest, configuration=context)
    finally:
        if owns_broker:
            await broker.aclose()
    return await asyncio.to_thread(_publish_freeze, settings, generation, costs, root, identity, checkpoint, soak_proof)


def bound_generation(settings: Settings) -> str | None:
    database = AsyncSQLiteDatabase(settings.database_url).path
    if not database.exists():
        return None
    # An ordinary database predating the identity migration may be inspected;
    # it can never pass freeze's strict legacy-database rejection.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verification_identity'").fetchone():
            if store_for(settings).exists():
                raise VerificationError("database_identity_missing")
            return None
    finally:
        connection.close()
    checkpoint = load_opening_checkpoint(database)
    return checkpoint["verification_generation_id"] if checkpoint is not None else None


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
            checkpoint = load_opening_checkpoint(self.database)
            if checkpoint is None or checkpoint["verification_generation_id"] != self.generation:
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
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
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
        checkpoint = load_opening_checkpoint(self.database)
        if (checkpoint is None or started.get("verification_generation_id") != self.generation
                or started.get("generation_opening_checkpoint_id") != checkpoint["checkpoint_id"]):
            raise VerificationError("start_record_generation_mismatch")
        if not max(timestamp(read_manifest(self.directory)["created_at"]), timestamp(checkpoint["opened_at"])) <= stamp <= datetime.now(UTC):
            raise VerificationError("start_record_timestamp_invalid")
        return started["started_at"]

    def begin(self) -> None:
        if self._lease is None or not self.check()["integrity_valid"]:
            raise VerificationError("generation_start_requires_valid_exclusive_lease")
        if (self.directory / "started.json").exists():
            self.started_at()
            return
        checkpoint = load_opening_checkpoint(self.database)
        started = {"started_at": utc_now(), "verification_generation_id": self.generation,
                   "generation_opening_checkpoint_id": checkpoint["checkpoint_id"]}
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
            current = snapshot_database(self.database)
            # The old seal is evidence of its own population, not permanent
            # broker finality. Audit/heartbeat appends have no financial effect.
            financial_tables = ('fills', 'settlements', 'position_lots', 'trade_attributions',
                                'trade_intents', 'orders', 'cash_ledger', 'pnl_records', 'accounting_epochs',
                                'integrity_holds')
            changed = [table for table in financial_tables if current[table] != evidence[table]]
            membership_state = {}
            from tradepulse.reconciliation.membership import ELIGIBLE_MEMBERSHIPS
            for row in reversed(current['reconciliation_records']):
                if row['reconciliation_type'] == 'generation_membership':
                    from .opening import load_opening_checkpoint
                    from tradepulse.reconciliation.membership import verify_membership_record
                    classifications = verify_membership_record(load_opening_checkpoint(self.database), row)
                    membership_state = classifications
                    sealed_ids = {a['id'] for r in evidence['reconciliation_records']
                                  if r['reconciliation_type'] == 'generation_membership'
                                  for a in r['actual']['activities']}
                    if any(status == 'unresolved_generation_membership'
                           or (identifier not in sealed_ids and status in ELIGIBLE_MEMBERSHIPS)
                           for identifier, status in classifications.items()):
                        changed.append('generation_membership')
                    break
            if changed:
                supersession = {'seal_sha256': digest(canonical(sealed)), 'changed_populations': sorted(changed),
                                'current_evidence_sha256': digest(canonical({
                                    **{table: current[table] for table in financial_tables},
                                    'membership': membership_state}))}
                path = self.directory / ('seal-superseded-' + digest(canonical(supersession)) + '.json')
                try:
                    write_once(path, supersession)
                except FileExistsError:
                    pass
                return {'status': 'PROVE_EDGE_FAILED_INTEGRITY', 'reason': 'sealed_generation_reopened',
                        'superseded_seal_sha256': supersession['seal_sha256'], 'integrity': integrity}
            if not integrity['integrity_valid']:
                return {'status': 'PROVE_EDGE_FAILED_INTEGRITY', 'integrity': integrity}
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

    async def start(self, *, reconciliation_only: bool = False) -> bool:
        try:
            await asyncio.to_thread(self.acquire)
            if not reconciliation_only and await asyncio.to_thread((self.directory / "seal.json").exists):
                raise VerificationError("generation_sealed_new_generation_required")
            if (await asyncio.to_thread((self.directory / "sealed-evidence.json").exists)
                    and not await asyncio.to_thread((self.directory / "seal.json").exists)):
                raise VerificationError("incomplete_seal_requires_audit")
            if await asyncio.to_thread((self.directory / "started.json").exists):
                await asyncio.to_thread(self.started_at)
            logger.info("paper_verification_guard_acquired", extra={"event": "paper_verification_guard_acquired", "generation": self.generation})
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            await asyncio.to_thread(self.release)
            logger.error("paper_verification_refused", extra={"event": "paper_verification_refused", "reason": str(exc)})
            return False

    async def runtime_started(self, broker=None) -> bool:
        """Called only after runtime activation succeeds, immediately before lanes."""
        owns_broker = broker is None
        try:
            if owns_broker:
                from tradepulse.session_commands import build_broker, require_credentials
                require_credentials(self.settings, require_ai=False)
                broker = build_broker(self.settings)
            account = await broker.get_account()
            from tradepulse.time import aware_utc
            aware_utc(account.received_at, field_name="startup_account_received_at")
            checkpoint = await asyncio.to_thread(load_opening_checkpoint, self.database)
            identity = {"account_id": account.account_id, "account_number": account.account_number}
            if checkpoint is None or digest(canonical(identity)) != checkpoint["account_identity_digest"]:
                raise VerificationError("startup_broker_account_identity_changed")
            await asyncio.to_thread(self.begin)
            logger.info("paper_verification_started", extra={"event": "paper_verification_started", "generation": self.generation})
            return True
        except Exception as exc:
            logger.error("paper_verification_start_refused", extra={"event": "paper_verification_start_refused", "reason": str(exc)})
            return False
        finally:
            if owns_broker and broker is not None:
                await broker.aclose()

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
