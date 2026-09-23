"""Disposable evidence reports; all broker receipts here are sanitized fixtures."""
import copy
import importlib.util
import os
import signal
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from python_tests.opening_fixtures import OpeningBroker
from tradepulse.config import Settings
from tradepulse.models import AuditEvent
from tradepulse.persistence import AsyncSQLiteDatabase
from tradepulse.persistence.repositories import RecordRepository
from tradepulse.verification.integrity import VerificationError, canonical, digest, load_json
from tradepulse.verification.opening import load_opening_checkpoint
from tradepulse.verification.service import Verification, freeze, store_for
from tradepulse.verification.soak import (
    AUTHORIZED_COSTS, MINIMUM_SECONDS, REQUIRED_LANES, _complete_market_session,
    _lane_evidence, _late_fee_reopenings, _segments, analyze_soak_database,
    create_soak_report, slippage_evidence, verify_soak_prerequisites,
)


@pytest.fixture
async def frozen_databases(tmp_path):
    root = tmp_path / "source"
    (root / "tradepulse").mkdir(parents=True)
    (root / "tradepulse" / "authority.py").write_text("AUTHORITY = 1\n")
    result = []
    for number in (1, 2):
        settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/soak-{number}.db"})
        database = AsyncSQLiteDatabase(settings.database_url)
        await database.initialize()
        manifest = await freeze(settings, f"soak-accounting-{number}", AUTHORIZED_COSTS, root, broker=OpeningBroker())
        result.append(SimpleNamespace(database=database, settings=settings, manifest=manifest, root=root,
                                      generation=f"soak-accounting-{number}", report=tmp_path / f"report-{number}.json"))
    return result


def verify(paths, frozen, **kwargs):
    return verify_soak_prerequisites(paths, source_digest=kwargs.get("source_digest", frozen.manifest["aggregate_sha256"]),
        context=kwargs.get("context", frozen.manifest["context"]), costs=kwargs.get("costs", AUTHORIZED_COSTS))


def rewrite_report(path, value):
    value = copy.deepcopy(value)
    value.pop("sha256", None)
    value["sha256"] = digest(canonical(value))
    path.write_bytes(canonical(value))


async def test_actual_empty_reports_are_preserved_hashable_and_never_pass(frozen_databases):
    first, second = frozen_databases
    original = first.database.path.read_bytes()
    reports = [create_soak_report(item.database.path, item.report, run_number=number)
               for number, item in enumerate(frozen_databases, 1)]
    for number, (item, report) in enumerate(zip(frozen_databases, reports), 1):
        assert report == load_json(item.report)
        body = {key: value for key, value in report.items() if key != "sha256"}
        assert report["sha256"] == digest(canonical(body))
        preserved = Path(report["database_path"])
        assert preserved != item.database.path and preserved.is_file()
        assert preserved.stat().st_mode & 0o777 == 0o600
        assert report["database_sha256"] == digest(preserved.read_bytes())
        assert load_opening_checkpoint(preserved) == load_opening_checkpoint(item.database.path)
        assert report["analysis"] == analyze_soak_database(preserved, run_number=number)
        assert report["analysis"]["status"] == "NOT_PASSED"
        assert report["analysis"]["duration_seconds"] == 0
        assert report["analysis"]["required_continuous_seconds"] == MINIMUM_SECONDS[number]
        assert report["analysis"]["counts"]["fills"] == 0
        assert report["analysis"]["guarded_start"] is None
        assert report["analysis"]["performance"]["observed_generation_net"] is None
        assert not report["analysis"]["invariants"]["all_lanes_continuous"]
        assert not report["analysis"]["invariants"]["fee_receipts_conserved"]
    assert first.database.path.read_bytes() == original
    with pytest.raises(VerificationError, match="incomplete_or_failed"):
        verify([first.report, second.report], first)
    with pytest.raises(VerificationError, match="already_exists"):
        create_soak_report(first.database.path, first.report, run_number=1)


async def test_failed_run_report_contains_real_incident_and_partial_runtime(frozen_databases):
    first, _ = frozen_databases
    guard = Verification(first.settings, first.generation, root=first.root)
    assert await guard.start()
    assert await guard.runtime_started(OpeningBroker())
    events = RecordRepository(first.database, "audit_events")
    start = datetime.now(UTC)
    for event in (
        AuditEvent("start", "verification_runtime_started", "info", "Fixture runtime start", start,
                   details={"generation": first.generation, "startup_id": "attempt-1", "first_start": True}),
        AuditEvent("failure", "trading_supervisor_lane_failed", "critical", "Fixture failed settlement", start,
                   details={"lane": "settle", "error": "sanitized fixture failure"}),
    ):
        await events.create_once(event.event_id, event)
    with pytest.raises(VerificationError, match="stop_soak_runtime"):
        create_soak_report(first.database.path, first.report, run_number=1)
    assert not first.report.exists()
    guard.release()
    report = create_soak_report(first.database.path, first.report, run_number=1)
    assert report["analysis"]["status"] == "NOT_PASSED"
    assert report["analysis"]["counts"]["integrity_incidents"] == 1
    assert report["analysis"]["incidents"][0]["event_id"] == "failure"
    assert report["analysis"]["runtime_boundary_errors"] == ["runtime_shutdown_evidence_incomplete"]


async def test_reused_database_cannot_supply_both_reports(frozen_databases, tmp_path):
    first, _ = frozen_databases
    create_soak_report(first.database.path, first.report, run_number=1)
    second_path = tmp_path / "renamed-report.json"
    create_soak_report(first.database.path, second_path, run_number=2)
    with pytest.raises(VerificationError, match="distinct_databases"):
        verify([first.report, second_path], first)


@pytest.mark.parametrize("change", ["digest", "forged_pass", "database", "wal", "source", "context", "account", "artifact"])
async def test_tampering_cannot_authorize_official_freeze(frozen_databases, change):
    first, second = frozen_databases
    report = create_soak_report(first.database.path, first.report, run_number=1)
    create_soak_report(second.database.path, second.report, run_number=2)
    expected = "digest_invalid"
    kwargs = {}
    if change == "digest":
        report["analysis"]["status"] = "PASSED"
        first.report.write_bytes(canonical(report))
    elif change == "forged_pass":
        report["analysis"]["status"] = "PASSED"
        report["analysis"]["invariants"] = {key: True for key in report["analysis"]["invariants"]}
        rewrite_report(first.report, report)
        expected = "incomplete_or_failed"
    elif change == "database":
        with sqlite3.connect(report["database_path"]) as connection:
            connection.execute("CREATE TABLE unrelated_mutation(value TEXT)")
        expected = "database_missing_or_changed"
    elif change == "wal":
        Path(report["database_path"] + "-wal").write_bytes(b"unhashed financial evidence")
        expected = "unhashed_journal"
    elif change == "artifact":
        preserved = Path(report["database_path"])
        directory = preserved.parent / (preserved.name + ".paper-verification") / first.generation
        (directory / "unreviewed.json").write_text("{}")
        expected = "artifacts_changed"
    else:
        expected = "manifest_or_account_binding_invalid"
        if change == "source":
            report["source_digest"] = "a" * 64
            kwargs["source_digest"] = report["source_digest"]
        elif change == "context":
            report["configuration"]["risk_profile"] = "micro"
            kwargs["context"] = report["configuration"]
        else:
            report["account_identity_digest"] = "b" * 64
        rewrite_report(first.report, report)
    with pytest.raises(VerificationError, match=expected):
        verify([first.report, second.report], first, **kwargs)


async def test_official_rates_and_current_context_must_match_both_soaks(frozen_databases):
    first, second = frozen_databases
    for number, item in enumerate(frozen_databases, 1):
        create_soak_report(item.database.path, item.report, run_number=number)
    paths = [first.report, second.report]
    with pytest.raises(VerificationError, match="requires_two"):
        verify([first.report], first)
    with pytest.raises(VerificationError, match="overlay_not_authorized"):
        verify(paths, first, costs={"fee_bps": "24", "slippage_bps": "15"})
    with pytest.raises(VerificationError, match="rerun_both_soaks"):
        verify(paths, first, source_digest="0" * 64)
    with pytest.raises(VerificationError, match="rerun_both_soaks"):
        verify(paths, first, context={**first.manifest["context"], "risk_profile": "micro"})


def event(kind, when, **details):
    return {"event_id": kind + when.isoformat(), "event_type": kind, "occurred_at": when.isoformat(), "details": details}


def test_lane_duration_and_restart_evidence_cannot_be_inferred_from_total_elapsed_time():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = start + timedelta(hours=12)
    restart = end + timedelta(minutes=1)
    stop = restart + timedelta(minutes=30)
    boundaries = [
        event("verification_runtime_started", start, generation="soak-test", startup_id="a", first_start=True),
        event("verification_runtime_stopped", end, generation="soak-test", startup_id="a"),
        event("verification_runtime_started", restart, generation="soak-test", startup_id="b", first_start=False),
        event("verification_runtime_stopped", stop, generation="soak-test", startup_id="b"),
    ]
    segments, errors = _segments(boundaries, "soak-test")
    assert not errors and segments[0]["duration_seconds"] == 12 * 3600
    assert segments[1]["duration_seconds"] == 30 * 60
    cycles = []
    for lane, interval in REQUIRED_LANES.items():
        for left, right in ((start, end), (restart, stop)):
            at = left
            while at <= right:
                cycles.append(event("verification_lane_cycle", at, lane=lane))
                at += timedelta(seconds=interval)
    assert all(row["continuous"] for row in _lane_evidence(cycles, segments).values())
    silent = [row for row in cycles if row["details"]["lane"] != "crypto"]
    assert not _lane_evidence(silent, segments)["crypto"]["continuous"]
    assert "runtime_shutdown_evidence_incomplete" in _segments(boundaries[:-1], "soak-test")[1]
    boundaries[2]["details"]["first_start"] = True
    assert "runtime_first_start_evidence_invalid" in _segments(boundaries, "soak-test")[1]


def test_complete_equity_session_requires_broker_clock_coverage_and_continuous_crypto():
    opened = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    closed = opened + timedelta(hours=6, minutes=30)
    segments = [{"started_at": (opened - timedelta(hours=1)).isoformat(),
                 "ended_at": (closed + timedelta(hours=1)).isoformat()}]
    clocks = []
    at = opened - timedelta(minutes=1)
    while at <= closed + timedelta(minutes=1):
        active = opened <= at < closed
        clocks.append(event("verification_broker_clock", at, timestamp=at.isoformat(),
            is_open=active, next_open=(opened if at < opened else opened + timedelta(days=1)).isoformat(),
            next_close=(closed if at < closed else closed + timedelta(days=1)).isoformat(), received_at=at.isoformat()))
        at += timedelta(minutes=1)
    lanes = {"crypto": {"continuous": True}}
    assert _complete_market_session(clocks, segments, lanes)["complete"]
    assert not _complete_market_session([clocks[0], clocks[1], clocks[-1]], segments, lanes)["complete"]
    assert not _complete_market_session(clocks[1:], segments, lanes)["complete"]
    assert not _complete_market_session(clocks[:-2], segments, lanes)["complete"]
    assert not _complete_market_session(clocks, segments, {"crypto": {"continuous": False}})["complete"]


def test_adverse_slippage_uses_direction_and_complete_aware_reference_receipts():
    def fill(identifier, side, price, **changes):
        return {"fill_id": identifier, "broker_fill_id": identifier, "side": side, "price": price,
                "asset": {"asset_class": "equity"}, "reference_price": "100",
                "reference_observed_at": "2026-01-05T09:30:00-05:00",
                "submitted_at": "2026-01-05T14:30:01Z", "filled_at": "2026-01-05T14:30:02Z", **changes}
    rows = [fill("buy", "buy", "100.15"), fill("sell", "sell", "99.85"), fill("improved", "sell", "101")]
    result = slippage_evidence(rows)
    assert result["coverage_complete"] and not result["review_required"]
    assert Decimal(result["by_side"]["buy"]["maximum_adverse_bps"]) == 15
    assert Decimal(result["samples"][-1]["adverse_bps"]) == -100
    result = slippage_evidence([*rows, fill("exceeded", "sell", "99.84")])
    assert result["review_required"] and result["by_side"]["sell"]["above_15_bps"] == 1
    result = slippage_evidence([*rows, fill("missing", "buy", "100", reference_observed_at=None)])
    assert not result["coverage_complete"] and result["missing_reference_fill_ids"] == ["missing"]
    assert not slippage_evidence([])["coverage_complete"]


def test_late_fee_requires_linked_preserved_prior_and_replacement_proofs():
    prior = {"accounting_epoch_id": "epoch", "checkpoint_id": "prior", "population_proof_id": "old-population"}
    current = {**prior, "checkpoint_id": "replacement", "population_proof_id": "new-population",
               "fee_accounting_status": "reconciled_net", "superseded_checkpoint_ids": ["prior"]}
    supersession = {"record_id": "supersession", "actual": {"superseded_checkpoint": prior}}
    rows = {"accounting_epochs": [current], "reconciliation_records": [
        {"record_id": "prior", "actual": prior},
        {"record_id": "old-population", "actual": {"activities": []}},
        {"record_id": "new-population", "actual": {"activities": [{"id": "fee", "activity_type": "FEE"}]}},
        {"record_id": "generation_fee:fee"},
    ]}
    result = _late_fee_reopenings(rows, [supersession])
    assert result[0]["new_fee_activity_ids"] == ["fee"]
    rows["reconciliation_records"].pop()
    assert not _late_fee_reopenings(rows, [supersession])


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_accounting_soak.py"
    spec = importlib.util.spec_from_file_location("accounting_soak_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_runner_command_uses_real_cli_module_without_broker_calls(runner, tmp_path):
    path = tmp_path / "command.log"
    with path.open("w") as log:
        assert await runner._command(dict(os.environ), log, "provenance") == 0
    assert path.read_text()


def test_runner_main_logging_and_configuration(runner, tmp_path):
    with pytest.raises(SystemExit) as exited:
        runner.main(["--help"])
    assert exited.value.code == 0
    args = runner.parser().parse_args(["--run-number", "1", "--database", str(tmp_path / "new.db"),
                                       "--report", str(tmp_path / "report.json")])
    assert runner.configuration(args)[2:] == ("soak-accounting-1", 12 * 3600)
    args.generation = "prove-edge-1"
    with pytest.raises(VerificationError, match="cannot_start_official"):
        runner.configuration(args)
    args.generation = "soak-fixture"
    args.database.write_bytes(b"prior evidence must never be cleaned")
    with pytest.raises(VerificationError, match="never_clean_or_reuse"):
        runner.configuration(args)


async def test_runner_session_passes_guarded_command_and_drains_on_stop(runner, monkeypatch, tmp_path):
    process = SimpleNamespace(returncode=None, signals=[])
    async def wait():
        process.returncode = 0
        return 0
    process.wait = wait
    process.send_signal = process.signals.append
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    counts = iter((0, 1))
    monkeypatch.setattr(runner, "_runtime_start_count", lambda _: next(counts))
    assert await runner._session({}, None, tmp_path / "soak.db", "soak-accounting-1", 8765, 0) == 0
    assert spawn.call_args.args == (sys.executable, "-m", "tradepulse.cli", "run", "--no-browser",
                                   "--port", "8765", "--verification-generation", "soak-accounting-1")
    assert process.signals == [signal.SIGINT]


async def test_runner_pipeline_preserves_report_and_never_starts_official(runner, monkeypatch, tmp_path):
    command = AsyncMock(return_value=0)
    session = AsyncMock(return_value=0)
    monkeypatch.setattr(runner, "_command", command)
    monkeypatch.setattr(runner, "_session", session)
    monkeypatch.setattr(runner, "create_soak_report", lambda *_args, **_kwargs:
                        {"sha256": "a" * 64, "analysis": {"status": "NOT_PASSED"}})
    args = runner.parser().parse_args(["--run-number", "2", "--database", str(tmp_path / "new.db"),
                                       "--report", str(tmp_path / "report.json")])
    assert await runner.run(args) == 1
    commands = [call.args[2:] for call in command.call_args_list]
    assert commands == [
        ("verification", "freeze", "--generation", "soak-accounting-2", "--fee-bps", "25", "--slippage-bps", "15"),
        ("reconcile", "--verification-generation", "soak-accounting-2"),
        ("reconcile", "--verification-generation", "soak-accounting-2"),
        ("verification", "status", "--generation", "soak-accounting-2"),
    ]
    assert [call.args[-1] for call in session.call_args_list] == [24 * 3600, 30 * 60]
    assert command.call_args_list[0].args[0]["TRADEPULSE_EXECUTION_MODE"] == "paper"
    assert command.call_args_list[0].args[0]["TRADEPULSE_LIVE_TRADING_ENABLED"] == "false"
    assert (tmp_path / "report.runtime.log").stat().st_mode & 0o777 == 0o600
