"""Canonical generation capture, immutable binding, and startup-clock proofs."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from python_tests.opening_fixtures import OpeningBroker
from tradepulse.config import Settings
from tradepulse.persistence import AsyncSQLiteDatabase
from tradepulse.time import aware_utc
from tradepulse.verification.integrity import VerificationError, canonical, digest, load_json
from tradepulse.verification.opening import CHECKPOINT_FILENAME, load_bound_opening_checkpoint, load_opening_checkpoint
from tradepulse.verification.service import Verification, freeze, store_for

OVERLAY = {"fee_bps": "25", "slippage_bps": "15"}


@pytest.fixture
async def fresh(tmp_path):
    root = tmp_path / "source"
    (root / "tradepulse").mkdir(parents=True)
    (root / "tradepulse" / "runtime.py").write_text("VALUE = 1\n")
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/isolated.db"})
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    return settings, root, database


async def test_freeze_captures_complete_history_and_binds_before_start(fresh):
    settings, root, database = fresh
    stamp = datetime.now(UTC) - timedelta(days=2)
    rows = [{"id": f"opening-{i:04}", "activity_type": "FILL", "transaction_time": stamp.isoformat(),
             "symbol": "FIX", "qty": "1", "price": "10", "side": "buy"} for i in range(101)]
    broker = OpeningBroker(rows)
    manifest = await freeze(settings, "soak-capture", OVERLAY, root, broker=broker)
    checkpoint = await database.run(load_bound_opening_checkpoint)
    assert checkpoint["activities"] == rows
    assert len(checkpoint["pagination"]["pages"]) == 2
    assert checkpoint["opening_activity_cursor"] == {"kind": "broker_activity", "last_activity_id": "opening-0100"}
    assert checkpoint["opening_activity_population_hash"] == checkpoint["pagination"]["population_hash"]
    assert checkpoint["cash"] == checkpoint["equity"] == "100000"
    assert checkpoint["positions"] == []
    assert manifest["generation_opening_checkpoint_sha256"] == digest(canonical(checkpoint))
    assert checkpoint["source_manifest_digest"] == manifest["aggregate_sha256"]
    assert checkpoint["configuration_digest"] == digest(canonical(manifest["context"]))
    identity = await database.run(lambda connection: dict(connection.execute("SELECT * FROM verification_identity").fetchone()))
    assert identity["generation_id"] == "soak-capture"
    assert identity["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert not (store_for(settings) / "soak-capture" / "started.json").exists()
    assert "sanitized" not in checkpoint["broker_credential_identity_digest"]


async def test_start_clock_requires_successful_runtime_and_survives_restart(fresh):
    settings, root, database = fresh
    broker = OpeningBroker()
    await freeze(settings, "soak-clock", OVERLAY, root, broker=broker)
    guard = Verification(settings, "soak-clock", root=root)
    assert await guard.start()
    assert not (guard.directory / "started.json").exists()
    assert await guard.runtime_started(broker)
    initial = (guard.directory / "started.json").read_bytes()
    checkpoint = await database.run(load_bound_opening_checkpoint)
    guard.release()
    second = Verification(settings, "soak-clock", root=root)
    assert await second.start()
    assert await second.runtime_started(broker)
    assert (second.directory / "started.json").read_bytes() == initial
    assert await database.run(load_bound_opening_checkpoint) == checkpoint
    second.release()


async def test_changed_account_refuses_start_and_never_starts_clock(fresh):
    settings, root, _ = fresh
    broker = OpeningBroker()
    await freeze(settings, "soak-account", OVERLAY, root, broker=broker)
    guard = Verification(settings, "soak-account", root=root)
    assert await guard.start()
    broker.account = replace(broker.account, account_id="other-account")
    assert not await guard.runtime_started(broker)
    assert not (guard.directory / "started.json").exists()
    guard.release()


@pytest.mark.parametrize("artifact", ["manifest.json", CHECKPOINT_FILENAME])
async def test_tampered_artifact_refuses_start_even_with_new_sidecar_digest(fresh, artifact):
    settings, root, _ = fresh
    await freeze(settings, "soak-tamper", OVERLAY, root, broker=OpeningBroker())
    guard = Verification(settings, "soak-tamper", root=root)
    path = guard.directory / artifact
    data = load_json(path)
    if artifact == "manifest.json":
        data["source_revision"] = "tampered"
        (guard.directory / "manifest-sha256.json").write_bytes(canonical({"sha256": digest(canonical(data))}))
    else:
        data["cash"] = "100001"
        data["checkpoint_id"] = digest(canonical({key: value for key, value in data.items() if key != "checkpoint_id"}))
    path.write_bytes(canonical(data))
    assert not await guard.start()


@pytest.mark.parametrize("field", ["account", "positions", "activity", "page", "duplicate", "incomplete", "identity"])
async def test_incomplete_or_naive_capture_never_binds(fresh, field):
    settings, root, database = fresh
    broker = OpeningBroker()
    if field == "account":
        broker.account = replace(broker.account, received_at=datetime(2026, 1, 1))
    elif field == "positions":
        broker.last_positions_received_at = None
    elif field == "activity":
        broker.activities = [{"id": "bad-time", "activity_type": "FILL", "transaction_time": "2026-01-01T12:00:00"}]
    elif field == "duplicate":
        row = {"id": "same", "activity_type": "FILL", "transaction_time": broker.now.isoformat()}
        broker.activities = [row, row]
    elif field == "identity":
        broker.account = replace(broker.account, account_id=None)
    else:
        original = broker.get_activities
        async def altered(*args, **kwargs):
            result = await original(*args, **kwargs)
            page = kwargs["page_evidence"][-1]
            if field == "page":
                page["received_at"] = "2026-01-01T00:00:00"
            else:
                page["terminal"] = False
            return result
        broker.get_activities = altered
    with pytest.raises(ValueError):
        await freeze(settings, "soak-invalid", OVERLAY, root, broker=broker)
    assert await database.run(load_bound_opening_checkpoint) is None
    assert not store_for(settings).exists()


async def test_preexisting_or_deleted_evidence_cannot_be_frozen(fresh):
    settings, root, database = fresh
    def historical(connection):
        connection.execute("INSERT INTO equity_snapshots VALUES ('old','{}','2026-01-01T00:00:00+00:00')")
        connection.execute("DELETE FROM equity_snapshots")
    await database.run(historical, write=True)
    with pytest.raises(VerificationError, match="fresh_evidence"):
        await freeze(settings, "soak-old", OVERLAY, root, broker=OpeningBroker())


async def test_existing_artifacts_and_rebinding_are_refused(fresh):
    settings, root, _ = fresh
    await freeze(settings, "soak-once", OVERLAY, root, broker=OpeningBroker())
    with pytest.raises(VerificationError, match="artifacts_already_exist"):
        await freeze(settings, "soak-twice", OVERLAY, root, broker=OpeningBroker())


async def test_renamed_legacy_database_is_not_new(tmp_path):
    path = tmp_path / "renamed-official.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE historical(value TEXT)")
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{path}"})
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    with pytest.raises(VerificationError, match="fresh_evidence"):
        await freeze(settings, "soak-old", OVERLAY, broker=OpeningBroker())


async def test_official_generation_requires_two_soak_reports(fresh):
    settings, root, _ = fresh
    with pytest.raises(VerificationError):
        await freeze(settings, "prove-edge-1", OVERLAY, root, broker=OpeningBroker())
    assert not store_for(settings).exists()


async def test_default_database_is_rejected(tmp_path):
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/tradepulse.db"})
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    with pytest.raises(VerificationError, match="new_dedicated"):
        await freeze(settings, "soak-default", OVERLAY, broker=OpeningBroker())


@pytest.mark.parametrize("value", [None, "", "bad", "2026-01-01", "2026-01-01T12:00:00", datetime(2026, 1, 1)])
def test_utc_requires_mandatory_timezone(value):
    with pytest.raises(ValueError):
        aware_utc(value)


def test_offset_timestamp_preserves_correct_instant():
    assert aware_utc("2026-09-22T09:30:00-04:00") == datetime(2026, 9, 22, 13, 30, tzinfo=UTC)
