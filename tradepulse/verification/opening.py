"""The immutable broker and database boundary for one verification generation.

Capture is asynchronous; artifact and SQLite inspection functions are synchronous
and must be invoked in a database worker or with ``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tradepulse.persistence.codec import encode_payload
from tradepulse.time import aware_utc

from .integrity import VerificationError, canonical, digest, generation_path, load_json, read_manifest

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_FILENAME = "generation-opening-checkpoint.json"


@dataclass(frozen=True, slots=True)
class GenerationOpeningCheckpoint:
    schema_version: int
    verification_generation_id: str
    checkpoint_id: str
    opened_at: str
    account_identity_digest: str
    activities: list[dict]
    opening_activity_cursor: dict
    opening_activity_population_hash: str
    pagination: dict
    positions: list[dict]
    cash: str
    equity: str
    account_received_at: str
    positions_received_at: str
    database_identity: str
    source_manifest_digest: str
    configuration_digest: str
    broker_credential_identity_digest: str
    account_receipt: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _number(value, field: str) -> str:
    try:
        number = Decimal(str(value))
    except ArithmeticError as exc:
        raise VerificationError(f"opening_{field}_invalid") from exc
    if not number.is_finite():
        raise VerificationError(f"opening_{field}_invalid")
    return str(number)


def activity_timestamp(raw: dict) -> datetime:
    """No date-only activity or substituted response time is financial evidence."""
    stamps = [aware_utc(raw[key], field_name=f"activity_{key}")
              for key in ("transaction_time", "created_at") if raw.get(key) is not None]
    if not stamps:
        raise VerificationError("opening_activity_timestamp_missing")
    return stamps[0]


def validate_checkpoint(checkpoint: dict) -> dict:
    from tradepulse.reconciliation.activity_cursor import validate_pagination

    if (set(checkpoint) != set(GenerationOpeningCheckpoint.__dataclass_fields__)
            or checkpoint["schema_version"] != CHECKPOINT_SCHEMA_VERSION):
        raise VerificationError("generation_opening_checkpoint_schema_invalid")
    identity = {key: value for key, value in checkpoint.items() if key != "checkpoint_id"}
    if checkpoint["checkpoint_id"] != digest(canonical(identity)):
        raise VerificationError("generation_opening_checkpoint_identity_invalid")
    opened = aware_utc(checkpoint["opened_at"], field_name="generation_opened_at")
    if opened > datetime.now(UTC):
        raise VerificationError("generation_opening_checkpoint_future")
    for field in ("account_received_at", "positions_received_at"):
        if aware_utc(checkpoint[field], field_name=field) > opened:
            raise VerificationError("opening_response_after_checkpoint")
    activities = checkpoint["activities"]
    if not isinstance(activities, list):
        raise VerificationError("opening_activities_invalid")
    ids = [raw.get("id") for raw in activities]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise VerificationError("opening_activity_identity_invalid")
    for raw in activities:
        if activity_timestamp(raw) > opened:
            raise VerificationError("opening_activity_after_checkpoint")
    validate_pagination(activities, checkpoint["pagination"])
    for page in checkpoint["pagination"]["pages"]:
        if aware_utc(page.get("received_at"), field_name="activity_page_received_at") > opened:
            raise VerificationError("opening_response_after_checkpoint")
    expected_cursor = {"kind": "broker_activity", "last_activity_id": ids[-1]} if ids else {
        "kind": "empty_population", "last_activity_id": None}
    if (checkpoint["opening_activity_cursor"] != expected_cursor
            or checkpoint["opening_activity_population_hash"] != checkpoint["pagination"]["population_hash"]):
        raise VerificationError("opening_population_boundary_invalid")
    if not isinstance(checkpoint["positions"], list):
        raise VerificationError("opening_positions_invalid")
    identities = set()
    for position in checkpoint["positions"]:
        identity = (position["asset_class"], position["symbol"])
        if not all(identity) or identity in identities:
            raise VerificationError("opening_position_identity_invalid")
        identities.add(identity)
        if aware_utc(position.get("received_at"), field_name="position_received_at").isoformat() != checkpoint["positions_received_at"]:
            raise VerificationError("opening_position_receipt_mismatch")
        for field in ("qty", "market_value", "avg_entry_price"):
            _number(position[field], field)
    for field in ("cash", "equity"):
        _number(checkpoint[field], field)
    for field in ("account_identity_digest", "source_manifest_digest", "configuration_digest",
                  "broker_credential_identity_digest"):
        from .integrity import DIGEST_PATTERN
        if not isinstance(checkpoint[field], str) or not DIGEST_PATTERN.fullmatch(checkpoint[field]):
            raise VerificationError(f"opening_{field}_invalid")
    receipt = checkpoint["account_receipt"]
    if not isinstance(receipt, dict) or not isinstance(receipt.get("account_id"), str) or not receipt["account_id"]:
        raise VerificationError("opening_account_identity_missing")
    if checkpoint["account_identity_digest"] != digest(canonical({
            "account_id": receipt["account_id"], "account_number": receipt.get("account_number")})):
        raise VerificationError("opening_account_identity_mismatch")
    return checkpoint


async def capture_checkpoint(broker, *, generation: str, database_identity: str,
                             source_manifest_digest: str, configuration: dict) -> dict:
    from tradepulse.reconciliation.activity_cursor import activity_population

    # Bracket account/position responses with complete history captures. Any
    # intervening activity invalidates the cut rather than guessing ownership.
    before, _ = await activity_population(broker)
    account = await broker.get_account()
    positions = await broker.get_positions()
    positions_received_at = aware_utc(getattr(broker, "last_positions_received_at", None),
                                     field_name="positions_received_at").isoformat()
    activities, pagination = await activity_population(broker)
    if before != activities:
        raise VerificationError("opening_activity_changed_during_capture")
    account_id = getattr(account, "account_id", None)
    if not isinstance(account_id, str) or not account_id.strip():
        raise VerificationError("opening_account_identity_missing")
    account_number = getattr(account, "account_number", None)
    receipt = json.loads(encode_payload(account))
    receipt["received_at"] = aware_utc(account.received_at, field_name="account_received_at").isoformat()
    position_receipts = []
    for position in positions:
        value = json.loads(encode_payload(position))
        value["received_at"] = aware_utc(position.received_at, field_name="position_received_at").isoformat()
        position_receipts.append(value)
    value = GenerationOpeningCheckpoint(
        schema_version=CHECKPOINT_SCHEMA_VERSION, verification_generation_id=generation, checkpoint_id="",
        opened_at=datetime.now(UTC).isoformat(),
        account_identity_digest=digest(canonical({"account_id": account_id, "account_number": account_number})),
        activities=activities, opening_activity_cursor={"kind": "broker_activity", "last_activity_id": activities[-1]["id"]} if activities else {
            "kind": "empty_population", "last_activity_id": None},
        opening_activity_population_hash=pagination["population_hash"], pagination=pagination,
        positions=position_receipts, cash=_number(account.cash, "cash"), equity=_number(account.equity, "equity"),
        account_received_at=receipt["received_at"], positions_received_at=positions_received_at,
        database_identity=database_identity, source_manifest_digest=source_manifest_digest,
        configuration_digest=digest(canonical(configuration)),
        broker_credential_identity_digest=configuration["broker_credentials_sha256"], account_receipt=receipt,
    ).as_dict()
    value["checkpoint_id"] = digest(canonical({key: item for key, item in value.items() if key != "checkpoint_id"}))
    return validate_checkpoint(value)


def database_identity(connection: sqlite3.Connection) -> dict:
    cursor = connection.execute("SELECT * FROM verification_identity WHERE singleton=1")
    row = cursor.fetchone()
    if row is None:
        raise VerificationError("database_identity_missing")
    return dict(zip((column[0] for column in cursor.description), row))


def load_bound_opening_checkpoint(connection: sqlite3.Connection) -> dict | None:
    """Validate one DB-bound artifact; never infer a generation from wall time."""
    identity = database_identity(connection)
    database = next((Path(row[2]).resolve() for row in connection.execute("PRAGMA database_list")
                     if row[1] == "main" and row[2]), None)
    if database is None:
        if identity["generation_id"] is None:
            return None
        raise VerificationError("verification_database_path_missing")
    store = database.parent / (database.name + ".paper-verification")
    if identity["generation_id"] is None:
        if store.exists():
            raise VerificationError("unbound_generation_artifacts_exist")
        return None
    directory = generation_path(store, identity["generation_id"])
    manifest = read_manifest(directory)
    manifest_digest = digest(canonical(manifest))
    if manifest_digest != identity["manifest_digest"]:
        raise VerificationError("database_manifest_binding_mismatch")
    if load_json(store / "binding.json") != {
            "generation": identity["generation_id"], "manifest_sha256": manifest_digest,
            "database_identity": identity["database_id"], "checkpoint_id": identity["checkpoint_id"]}:
        raise VerificationError("generation_binding_mismatch")
    checkpoint = validate_checkpoint(load_json(directory / CHECKPOINT_FILENAME))
    if (manifest.get("generation_opening_checkpoint_sha256") != digest(canonical(checkpoint))
            or checkpoint["checkpoint_id"] != identity["checkpoint_id"]
            or checkpoint["database_identity"] != identity["database_id"]
            or checkpoint["verification_generation_id"] != identity["generation_id"]
            or checkpoint["source_manifest_digest"] != manifest["aggregate_sha256"]
            or checkpoint["configuration_digest"] != digest(canonical(manifest["context"]))
            or checkpoint["broker_credential_identity_digest"] != manifest["context"]["broker_credentials_sha256"]):
        raise VerificationError("generation_opening_checkpoint_binding_mismatch")
    if (directory / "invalid.json").exists():
        raise VerificationError("generation_previously_invalidated")
    return checkpoint


def load_opening_checkpoint(database: Path) -> dict | None:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("BEGIN")
        return load_bound_opening_checkpoint(connection)
    finally:
        connection.close()


def load_bound_closing_checkpoint(connection: sqlite3.Connection) -> dict | None:
    """Return only the immutable activity population sealed by this generation."""
    checkpoint = load_bound_opening_checkpoint(connection)
    if checkpoint is None:
        return None
    database = next(Path(row[2]).resolve() for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    directory = generation_path(database.parent / (database.name + ".paper-verification"),
                                checkpoint["verification_generation_id"])
    seal_path = directory / "seal.json"
    if not seal_path.exists():
        if (directory / "sealed-evidence.json").exists():
            raise VerificationError("incomplete_seal_requires_audit")
        return None
    seal = load_json(seal_path)
    evidence = load_json(directory / "sealed-evidence.json")
    if (load_json(directory / "seal-sha256.json") != {"sha256": digest(canonical(seal))}
            or seal["evidence_sha256"] != digest(canonical(evidence))
            or seal["manifest_sha256"] != digest(canonical(read_manifest(directory)))):
        raise VerificationError("sealed_evidence_digest_mismatch")
    records = [row for row in evidence["reconciliation_records"]
               if row["reconciliation_type"] == "generation_membership"
               and row["subject_id"] == checkpoint["verification_generation_id"]]
    if not records:
        raise VerificationError("sealed_membership_evidence_missing")
    record = max(records, key=lambda row: aware_utc(row["occurred_at"], field_name="membership_received_at"))
    from tradepulse.reconciliation.membership import verify_membership_record
    verify_membership_record(checkpoint, record)
    actual = record["actual"]
    activities = actual["activities"]
    return {"sealed_at": aware_utc(seal["sealed_at"], field_name="sealed_at").isoformat(),
            "activities": activities, "cursor": {"kind": "broker_activity", "last_activity_id": activities[-1]["id"]} if activities else {
                "kind": "empty_population", "last_activity_id": None},
            "population_hash": actual["population_hash"]}
