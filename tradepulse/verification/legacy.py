"""Create-once seals for repaired databases that must never become generations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .evidence import snapshot_database
from .integrity import VerificationError, canonical, digest, load_json, write_once
from .opening import database_identity

SEAL_FILENAME = "legacy-evidence-seal.json"
SEAL_DIGEST_FILENAME = "legacy-evidence-seal-sha256.json"


def legacy_store(database: Path) -> Path:
    return database.parent / (database.name + ".legacy-evidence")


def seal_legacy_database(database: Path) -> dict:
    database = database.resolve()
    store = legacy_store(database)
    seal_path = store / SEAL_FILENAME
    if seal_path.exists() or (store / SEAL_DIGEST_FILENAME).exists():
        raise VerificationError("legacy_evidence_already_sealed")
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        identity = database_identity(connection)
        if identity["legacy_database"] != 1 or identity["generation_id"] is not None:
            raise VerificationError("legacy_seal_requires_unbound_legacy_database")
        if connection.execute("SELECT 1 FROM integrity_holds LIMIT 1").fetchone() is not None:
            raise VerificationError("legacy_seal_requires_zero_integrity_holds")
        unfinished = connection.execute(
            "SELECT 1 FROM accounting_epochs WHERE json_extract(payload,'$.fee_accounting_status') != 'reconciled_net' LIMIT 1"
        ).fetchone()
        if unfinished is not None:
            raise VerificationError("legacy_seal_requires_reconciled_accounting_epochs")
        evidence = snapshot_database(database)
        body = {
            "schema": "tradepulse-legacy-evidence-seal-v1",
            "database_identity": identity["database_id"],
            "database_path": str(database),
            "sealed_evidence_sha256": digest(canonical(evidence)),
            "evidence": evidence,
            "eligibility": "historical_pre_generation",
            "official_generation_eligible": False,
        }
        write_once(seal_path, body)
        write_once(store / SEAL_DIGEST_FILENAME, {"sha256": digest(canonical(body))})
        return body
    finally:
        connection.close()


def verify_legacy_seal(database: Path) -> dict:
    store = legacy_store(database.resolve())
    body = load_json(store / SEAL_FILENAME)
    if load_json(store / SEAL_DIGEST_FILENAME) != {"sha256": digest(canonical(body))}:
        raise VerificationError("legacy_evidence_seal_digest_mismatch")
    if body.get("official_generation_eligible") is not False:
        raise VerificationError("legacy_evidence_seal_eligibility_invalid")
    return body