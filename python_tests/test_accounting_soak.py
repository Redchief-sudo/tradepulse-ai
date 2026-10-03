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
from tradepulse.verification.service import Verification, freeze
from tradepulse.verification.soak import (
    AUTHORIZED_COSTS,
    MINIMUM_SECONDS,
    REQUIRED_LANES,
    _complete_market_session,
    _lane_evidence,
    _late_fee_reopenings,
    _segments,
    analyze_soak_database,
    create_soak_report,
    slippage_evidence,
    verify_soak_prerequisites,
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


def test_broker_clock_tolerates_bounded_server_lead_but_refuses_future_receipts():
    opened = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    closed = opened + timedelta(hours=6, minutes=30)
    segments = [{"started_at": (opened - timedelta(hours=1)).isoformat(),
                 "ended_at": (closed + timedelta(hours=1)).isoformat()}]
    lead = timedelta(milliseconds=45)
    clocks = []
    at = opened - timedelta(minutes=1)
    while at <= closed + timedelta(minutes=1):
        clocks.append(event("verification_broker_clock", at, timestamp=(at + lead).isoformat(),
            is_open=opened <= at < closed,
            next_open=(opened if at < opened else opened + timedelta(days=1)).isoformat(),
            next_close=(closed if at < closed else closed + timedelta(days=1)).isoformat(), received_at=at.isoformat()))
        at += timedelta(minutes=1)
    lanes = {"crypto": {"continuous": True}}
    assert _complete_market_session(clocks, segments, lanes)["complete"]
    future = clocks[0]["details"] | {"timestamp": (opened + timedelta(minutes=1)).isoformat()}
    with pytest.raises(VerificationError, match="invalid_soak_broker_clock"):
        _complete_market_session([{**clocks[0], "details": future}, *clocks[1:]], segments, lanes)


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
    module._real_broker_preflight = module._broker_preflight
    module._real_verify_opening_account = module._verify_opening_account
    module._broker_preflight = AsyncMock()
    module._verify_opening_account = AsyncMock()
    module._service_active = AsyncMock(return_value="inactive")  # tests never call the real systemctl
    module._load_dotenv = lambda *args, **kwargs: None  # never leak a real .env into the pytest process
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


def test_soak_lane_criteria_match_runtime_intervals():
    from tradepulse import cli
    from tradepulse.config.lanes import LANE_INTERVAL_SECONDS
    from tradepulse.verification.soak import REQUIRED_LANES

    assert REQUIRED_LANES == LANE_INTERVAL_SECONDS
    assert (cli.EQUITY_SCAN_INTERVAL_SECONDS, cli.CRYPTO_SCAN_INTERVAL_SECONDS, cli.OPTION_SCAN_INTERVAL_SECONDS,
            cli.MONITOR_INTERVAL_SECONDS, cli.SETTLE_INTERVAL_SECONDS, cli.RECONCILE_INTERVAL_SECONDS) == tuple(
        LANE_INTERVAL_SECONDS[k] for k in ("equity", "crypto", "option", "monitor", "settle", "reconcile"))
    assert cli.VERIFICATION_RECONCILE_INTERVAL_SECONDS == LANE_INTERVAL_SECONDS["reconcile"]
    assert LANE_INTERVAL_SECONDS["monitor"] == 30


def test_monitor_stall_over_two_minutes_breaks_continuity():
    from tradepulse.config.lanes import LANE_MAX_GAP_SECONDS
    from tradepulse.verification.soak import _lane_evidence

    assert LANE_MAX_GAP_SECONDS["monitor"] == 120
    assert LANE_MAX_GAP_SECONDS["reconcile"] == 2 * 60 + 120  # unchanged for other lanes
    start = datetime(2026, 10, 2, 14, tzinfo=UTC)

    def cycles(offsets):
        return [{"event_type": "verification_lane_cycle", "details": {"lane": "monitor"},
                 "occurred_at": (start + timedelta(seconds=s)).isoformat()} for s in offsets]

    segment = [{"started_at": start.isoformat(), "ended_at": (start + timedelta(seconds=600)).isoformat()}]
    healthy = _lane_evidence(cycles(range(30, 600, 30)), segment)["monitor"]
    stalled = _lane_evidence(cycles([30, 60, 210, *range(240, 600, 30)]), segment)["monitor"]
    assert healthy["continuous"] and healthy["maximum_permitted_gap_seconds"] == 120
    assert not stalled["continuous"]  # a 150 s silence


def _preflight_broker(activities, open_orders=(), account_number="PA1"):
    broker = AsyncMock()
    broker.get_open_orders.return_value = list(open_orders)
    broker.get_activities.return_value = [SimpleNamespace(raw=a) for a in activities]
    broker.get_account.return_value = SimpleNamespace(account_id="acct-1", account_number=account_number)
    return broker


PREFLIGHT_NOW = datetime(2026, 10, 2, 14, tzinfo=UTC)  # 10:00 ET, 2026-10-02
OPTION_BUY_0929 = {"id": "20260929135313600::a", "activity_type": "FILL", "symbol": "IWM261030C00286000", "side": "buy"}
OCC_0929 = {"id": "20260929000000000::b", "activity_type": "FEE", "activity_sub_type": "OCC"}
SELL_0929 = {"id": "20260929094227714::c", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
REG_0929 = {"id": "20260929000000000::d", "activity_type": "FEE", "activity_sub_type": "REG"}


async def test_preflight_clear_with_complete_fee_evidence(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW)
    assert result["problems"] == []
    assert result["account"] == {"account_id": "acct-1", "account_number": "PA1"}


async def test_preflight_refuses_partial_fee_batch(runner):
    # OCC posted, but REG for the same day's sell not yet: a partial batch
    result = await runner.preflight(_preflight_broker([OCC_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["FEE_EVIDENCE_MISSING:20260929:REG"]


async def test_preflight_refuses_older_unresolved_fee_day(runner):
    old_sell = {"id": "20260924093005873::e", "activity_type": "FILL", "symbol": "GOOGL", "side": "sell"}
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929, old_sell]),
                                    now=PREFLIGHT_NOW)
    assert result["problems"] == ["FEE_EVIDENCE_MISSING:20260924:REG"]


async def test_preflight_refuses_a_day_whose_batch_cannot_have_run(runner):
    # a late receipt for today's fills cannot exist yet: today is refused even with a REG row present
    today_sell = {"id": "20261002094100000::f", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
    today_reg = {"id": "20261002000000000::g", "activity_type": "FEE", "activity_sub_type": "REG"}
    result = await runner.preflight(_preflight_broker([today_reg, today_sell]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["PRE_GENERATION_TRADE_TODAY", "FEE_DAY_NOT_CLOSED:20261002"]


async def test_preflight_acknowledged_day_is_recorded_not_refused(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW,
                                    acknowledged=frozenset({"20260929"}))
    assert result["problems"] == []
    assert result["fee_days"]["20260929"]["acknowledged"] is True


async def test_acknowledgement_never_waives_today_or_same_day_trading(runner):
    today_sell = {"id": "20261002094100000::f", "activity_type": "FILL", "symbol": "AAPL", "side": "sell"}
    result = await runner.preflight(_preflight_broker([today_sell]), now=PREFLIGHT_NOW, acknowledged=frozenset({"20261002"}))
    assert result["problems"] == ["PRE_GENERATION_TRADE_TODAY", "FEE_DAY_NOT_CLOSED:20261002",
                                  "ACKNOWLEDGEMENT_INVALID:20261002"]


async def test_acknowledgement_of_an_unknown_or_malformed_day_is_refused(runner):
    result = await runner.preflight(_preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]), now=PREFLIGHT_NOW,
                                    acknowledged=frozenset({"20260930", "2026-09-29"}))
    assert result["problems"] == ["ACKNOWLEDGEMENT_INVALID:2026-09-29", "ACKNOWLEDGEMENT_INVALID:20260930"]


async def test_preflight_json_is_the_result_object(runner, tmp_path, monkeypatch):
    import json

    broker = _preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929])
    monkeypatch.setattr("tradepulse.session_commands.build_broker", lambda settings: broker)
    environment = {"TRADEPULSE_EXECUTION_MODE": "paper", "TRADEPULSE_LIVE_TRADING_ENABLED": "false",
                   "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}
    account = await runner._real_broker_preflight(environment, tmp_path / "soak-accounting-1.json", frozenset())
    stored = json.loads((tmp_path / "soak-accounting-1.preflight.json").read_text())
    assert isinstance(stored, dict) and stored["problems"] == [] and stored["account"] == account


async def test_two_reports_in_one_directory_each_get_preflight_evidence(runner, tmp_path, monkeypatch):
    import json

    broker = _preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929])
    monkeypatch.setattr("tradepulse.session_commands.build_broker", lambda settings: broker)
    environment = {"TRADEPULSE_EXECUTION_MODE": "paper", "TRADEPULSE_LIVE_TRADING_ENABLED": "false",
                   "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}
    for number in (1, 2):
        await runner._real_broker_preflight(environment, tmp_path / f"soak-accounting-{number}.json", frozenset())
    files = sorted(tmp_path.glob("*.preflight.json"))
    assert [f.name for f in files] == ["soak-accounting-1.preflight.json", "soak-accounting-2.preflight.json"]
    assert all(isinstance(json.loads(f.read_text()), dict) for f in files)


async def test_preflight_refuses_open_orders(runner):
    result = await runner.preflight(_preflight_broker([], open_orders=[object()]), now=PREFLIGHT_NOW)
    assert result["problems"] == ["OPEN_BROKER_ORDERS"]


async def test_runner_refuses_opening_on_a_different_account(runner, tmp_path, monkeypatch):
    checkpoint = {"account_identity_digest": digest(canonical({"account_id": "acct-2", "account_number": "PA2"}))}
    monkeypatch.setattr(runner, "load_opening_checkpoint", lambda database: checkpoint)
    with pytest.raises(runner.VerificationError, match="soak_opening_account_mismatch"):
        await runner._real_verify_opening_account(tmp_path / "soak.db", {"account_id": "acct-1", "account_number": "PA1"})


async def test_runner_fixture_never_loads_a_dotenv_into_the_pytest_environment(runner, monkeypatch, tmp_path):
    monkeypatch.delenv("SOAK_FIXTURE_SENTINEL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOAK_FIXTURE_SENTINEL=leaked\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_command", AsyncMock(return_value=0))
    monkeypatch.setattr(runner, "_session", AsyncMock(return_value=0))
    monkeypatch.setattr(runner, "create_soak_report", lambda *_a, **_k: {"sha256": "a" * 64, "analysis": {"status": "NOT_PASSED"}})
    args = runner.parser().parse_args(["--run-number", "2", "--database", str(tmp_path / "new.db"),
                                       "--report", str(tmp_path / "report.json")])
    await runner.run(args)
    assert "SOAK_FIXTURE_SENTINEL" not in os.environ


_PAPER_ENV = {"TRADEPULSE_EXECUTION_MODE": "paper", "TRADEPULSE_LIVE_TRADING_ENABLED": "false",
              "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"}


async def test_preflight_refuses_when_the_supervised_service_is_active(runner, tmp_path, monkeypatch):
    import json

    built = []
    monkeypatch.setattr("tradepulse.session_commands.build_broker", lambda settings: built.append(settings))
    runner._service_active = AsyncMock(return_value="active")
    with pytest.raises(VerificationError, match="soak_preflight_refused:SUPERVISED_SERVICE_ACTIVE"):
        await runner._real_broker_preflight(dict(_PAPER_ENV), tmp_path / "soak-accounting-1.json", frozenset())
    stored = json.loads((tmp_path / "soak-accounting-1.preflight.json").read_text())
    assert stored["problems"] == ["SUPERVISED_SERVICE_ACTIVE"]
    assert built == []  # refused before any broker was built


async def test_preflight_proceeds_when_the_supervised_service_is_inactive(runner, tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("tradepulse.session_commands.build_broker",
                        lambda settings: _preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]))
    account = await runner._real_broker_preflight(dict(_PAPER_ENV), tmp_path / "soak-accounting-1.json", frozenset())
    stored = json.loads((tmp_path / "soak-accounting-1.preflight.json").read_text())
    assert stored["problems"] == [] and stored["account"] == account and stored["service_check"] == "inactive"


async def test_preflight_proceeds_with_evidence_note_when_systemctl_is_missing(runner, tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("tradepulse.session_commands.build_broker",
                        lambda settings: _preflight_broker([OCC_0929, REG_0929, SELL_0929, OPTION_BUY_0929]))
    runner._service_active = AsyncMock(return_value="systemctl_unavailable")
    await runner._real_broker_preflight(dict(_PAPER_ENV), tmp_path / "soak-accounting-1.json", frozenset())
    stored = json.loads((tmp_path / "soak-accounting-1.preflight.json").read_text())
    assert stored["problems"] == [] and stored["service_check"] == "systemctl_unavailable"


async def test_service_active_maps_systemctl_results_without_running_it(runner, monkeypatch):
    real = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "soak_real", Path(runner.__file__)))
    real.__spec__.loader.exec_module(real)

    def fake(returncode=None, missing=False):
        async def spawn(*args, **kwargs):
            if missing:
                raise FileNotFoundError("systemctl")
            assert args == ("systemctl", "--user", "is-active", "--quiet", "tradepulse-run")
            return SimpleNamespace(wait=AsyncMock(return_value=returncode))
        return spawn

    monkeypatch.setattr(real.asyncio, "create_subprocess_exec", fake(0))
    assert await real._service_active() == "active"
    monkeypatch.setattr(real.asyncio, "create_subprocess_exec", fake(3))
    assert await real._service_active() == "inactive"
    monkeypatch.setattr(real.asyncio, "create_subprocess_exec", fake(missing=True))
    assert await real._service_active() == "systemctl_unavailable"
