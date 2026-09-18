"""Isolated source trees and persisted fixtures; never a live prove-edge claim."""
import asyncio
import copy
import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradepulse.config import Settings
from tradepulse.persistence.database import SCHEMA
from tradepulse.verification.commands import permit_command, run_official
from tradepulse.verification.evidence import TABLES, VerificationPolicy, assess
from tradepulse.verification.integrity import (
    VerificationError, canonical, digest, freeze_source, read_manifest, source_files, verify_source, write_once,
)
from tradepulse.verification.service import Verification, freeze, store_for


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "source"
    (root / "tradepulse").mkdir(parents=True)
    (root / "tradepulse" / "policy.py").write_text("STOP = 1\n")
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    return root


def frozen_tree(tree, tmp_path):
    directory = tmp_path / "generations" / "paper-1"
    freeze_source(tree, directory, "paper-1", context={}, policy={}, revision="test")
    return directory


def test_deterministic_source_fingerprint_and_order(tree, tmp_path):
    first = frozen_tree(tree, tmp_path)
    second = tmp_path / "generations" / "paper-2"
    freeze_source(tree, second, "paper-2", context={}, policy={}, revision="test")
    assert read_manifest(first)["aggregate_sha256"] == read_manifest(second)["aggregate_sha256"]
    assert list(read_manifest(first)["protected_files"]) == sorted(source_files(tree))
    with pytest.raises(VerificationError, match="already_exists"):
        freeze_source(tree, first, "paper-1", context={}, policy={}, revision="test")


@pytest.mark.parametrize("change,field", [("modified", "modified"), ("deleted", "missing"), ("added", "added")])
def test_exact_source_differences(tree, tmp_path, change, field):
    directory = frozen_tree(tree, tmp_path)
    old = (directory / "manifest.json").read_bytes()
    path = tree / "tradepulse" / "policy.py"
    if change == "modified":
        path.write_text("STOP = 2\n")
    elif change == "deleted":
        path.unlink()
    else:
        path = tree / "tradepulse" / "new.py"
        path.write_text("NEW = True\n")
    result = verify_source(tree, directory)
    assert result["integrity_valid"] is False
    assert result[field] == [path.relative_to(tree).as_posix()]
    assert (directory / "manifest.json").read_bytes() == old


def test_runtime_evidence_stays_writable_and_excluded(tree, tmp_path):
    directory = frozen_tree(tree, tmp_path)
    for name in ("logs/run.log", "run.db", "run.db-wal", "data/report.json", "tradepulse/__pycache__/policy.pyc"):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mutable evidence")
    assert verify_source(tree, directory)["integrity_valid"]


@pytest.fixture
def generation(tree, tmp_path, monkeypatch):
    monkeypatch.setattr("tradepulse.verification.integrity.utc_now", lambda: START.isoformat())
    database = tmp_path / "paper.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{database}"})
    freeze(settings, "paper-1", {"fee_bps": "1", "slippage_bps": "1"}, tree)
    return Verification(settings, "paper-1", root=tree)


@pytest.mark.parametrize("bad", ["missing", "corrupt", "incomplete", "hash"])
async def test_invalid_manifest_refuses_start_without_rewriting(generation, bad):
    path = generation.directory / "manifest.json"
    if bad == "missing":
        path.unlink()
    elif bad == "corrupt":
        path.write_text("{")
    elif bad == "incomplete":
        path.write_text("{}")
    else:
        manifest = json.loads(path.read_text())
        manifest["aggregate_sha256"] = "0" * 64
        path.write_bytes(canonical(manifest))
    before = path.read_bytes() if path.exists() else None
    assert not await generation.start()
    assert (path.read_bytes() if path.exists() else None) == before


async def test_restart_and_sticky_invalidation(generation):
    assert await generation.start()
    initial = (generation.directory / "started.json").read_bytes()
    generation.release()
    restarted = Verification(generation.settings, "paper-1", root=generation.root)
    assert await restarted.start()
    assert (generation.directory / "started.json").read_bytes() == initial
    path = generation.root / "tradepulse/policy.py"
    original = path.read_bytes()
    path.write_text("changed")
    assert not restarted.check()["integrity_valid"]
    path.write_bytes(original)
    assert not restarted.check()["integrity_valid"]
    restarted.release()


async def test_paper_only_and_unlocked_bypass_refused(generation):
    live = Verification(replace(generation.settings, execution_mode="live", live_trading_enabled=True), "paper-1", root=generation.root)
    assert not await live.start()
    assert not permit_command(generation.settings, "run", None)
    assert not permit_command(generation.settings, "scan", None)


async def test_missing_generation_never_invokes_trading(tmp_path):
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/empty.db"})
    async def forbidden(_):
        pytest.fail("trading must not start")
    assert await run_official(settings, "missing", forbidden) == 1


async def test_runtime_source_failure_requests_graceful_shutdown(generation):
    assert await generation.start()
    (generation.root / "tradepulse/policy.py").write_text("changed")
    shutdown = asyncio.Event()
    await generation.watch(shutdown)
    assert shutdown.is_set()
    generation.release()


START = datetime(2025, 1, 1, tzinfo=UTC)
NOW = START + timedelta(days=61)
COSTS = {"fee_bps": "1", "slippage_bps": "1"}
ASSET = {"symbol": "X", "asset_class": "equity", "native_asset_id": "alpaca:X", "venue": None, "metadata": {}}


def passing_rows(count=200, wins=120):
    rows = {table: [] for table in TABLES}
    for i in range(count):
        buy, sell, lot_id, intent = f"b{i}", f"s{i}", f"lot{i}", f"intent{i}"
        entry_at = (START + timedelta(days=1)).isoformat()
        exit_at = (START + timedelta(days=60)).isoformat()
        price, pnl = ("102", "2") if i < wins else ("99", "-1")
        for fid, side, px, stamp, ti in ((buy, "buy", "100", entry_at, intent), (sell, "sell", price, exit_at, f"exit{i}")):
            rows["fills"].append({"fill_id": fid, "trade_intent_id": ti, "order_id": f"order{fid}", "asset": ASSET,
                                  "side": side, "execution_mode": "paper", "quantity": "1", "price": px,
                                  "fees": "0", "slippage": "0", "filled_at": stamp, "broker_fill_id": fid})
            rows["settlements"].append({"fill_id": fid, "status": "completed", **{key: True for key in (
                "lot_projected", "attribution_projected", "cash_projected", "holding_projected", "trade_projected", "integrity_verified")}})
        rows["trade_intents"].append({"trade_intent_id": intent, "status": "filled", "filled_quantity": "1"})
        rows["position_lots"].append({"lot_id": lot_id, "originating_fill_id": buy, "asset": ASSET,
                                      "opened_quantity": "1", "remaining_quantity": "0", "acquisition_price": "100",
                                      "closures": {sell: "1"}, "realized_pnl": pnl})
        rows["trade_attributions"].append({"lot_id": lot_id, "closing_fill_id": sell, "asset": ASSET,
                                           "quantity": "1", "opening_trade_intent_id": intent, "closing_trade_intent_id": f"exit{i}",
                                           "entry_price": "100", "exit_price": price, "realized_pnl": pnl})
    rows["equity_snapshots"] = [
        {"snapshot_id": "first", "as_of": START.isoformat(), "source": "broker", "total_equity": "10000"},
        {"snapshot_id": "last", "as_of": NOW.isoformat(), "source": "broker", "total_equity": "10160"},
    ]
    rows["reconciliation_records"] = [{"record_id": "r", "reconciliation_type": "position_accounting", "subject_id": "X",
                                       "outcome": "matched", "occurred_at": NOW.isoformat()}]
    # Full persisted contracts for the database-backed seal test.
    for row in rows["settlements"]:
        fill = next(f for f in rows["fills"] if f["fill_id"] == row["fill_id"])
        row.update({"settlement_event_id": row["fill_id"], "trade_intent_id": fill["trade_intent_id"],
                    "asset": ASSET, "side": fill["side"], "execution_mode": "paper", "quantity": "1",
                    "price": fill["price"], "occurred_at": fill["filled_at"]})
    for row in rows["trade_intents"]:
        row.update({"idempotency_key": row["trade_intent_id"], "correlation_id": "opportunity",
                    "asset": ASSET, "side": "buy", "execution_mode": "paper", "strategy": "test",
                    "created_at": (START + timedelta(days=1)).isoformat(), "requested_quantity": "1"})
    for row in rows["position_lots"]:
        row.update({"position_side": "long", "opened_at": (START + timedelta(days=1)).isoformat()})
    for row in rows["trade_attributions"]:
        row.update({"attribution_id": row["lot_id"] + ":" + row["closing_fill_id"],
                    "entry_at": (START + timedelta(days=1)).isoformat(),
                    "exit_at": (START + timedelta(days=60)).isoformat(), "created_at": NOW.isoformat()})
    for row in rows["equity_snapshots"]:
        row.update({"cash_balance": row["total_equity"], "holdings_value": "0", "sector_exposure": {},
                    "open_positions": 0, "outstanding_orders": 0, "trades_today": 0, "daily_pnl_pct": "0"})
    for row in rows["reconciliation_records"]:
        row.update({"expected": {}, "actual": {}})
    return rows


def test_all_gates_pass_from_completed_population():
    result = assess(passing_rows(), START.isoformat(), NOW, COSTS)
    assert result["status"] == "PROVE_EDGE_PASSED"
    assert all(row["passed"] for row in result["criteria"].values())
    assert result["criteria"]["eligible_round_trips"]["actual"] == 200


@pytest.mark.parametrize("defect", ["sample", "duration", "win_rate", "drawdown", "zero_pnl", "negative_pnl",
                                    "reconciliation", "settlement", "integrity", "missing", "duplicate", "costs"])
def test_each_failed_gate_prevents_pass(defect):
    rows = passing_rows(199 if defect == "sample" else 200, 109 if defect == "win_rate" else 120)
    now, costs = NOW, COSTS
    if defect == "duration":
        # Move the actual start rather than creating future-dated fixture evidence.
        result = assess(rows, (START + timedelta(days=3)).isoformat(), now, costs)
        assert not result["criteria"]["duration_days"]["passed"]
        return
    if defect == "drawdown":
        rows["equity_snapshots"].insert(1, {"snapshot_id": "trough", "as_of": (START + timedelta(days=30)).isoformat(),
                                           "source": "broker", "total_equity": "8800"})
    if defect in {"zero_pnl", "negative_pnl"}:
        for row in rows["trade_attributions"]:
            row["realized_pnl"] = "0" if defect == "zero_pnl" else "-1"
        for row in rows["position_lots"]:
            row["realized_pnl"] = "0" if defect == "zero_pnl" else "-1"
        costs = {"fee_bps": "0", "slippage_bps": "0"}
    if defect == "reconciliation":
        rows["reconciliation_records"][0]["outcome"] = "drift_detected"
    if defect == "settlement":
        rows["settlements"][0]["integrity_verified"] = False
    if defect == "integrity":
        rows["integrity_holds"].append({"hold_type": "verification_pending"})
    if defect == "missing":
        rows["trade_attributions"].pop()
    if defect == "duplicate":
        rows["trade_attributions"].append(copy.deepcopy(rows["trade_attributions"][0]))
    if defect == "costs":
        costs = None
    result = assess(rows, START.isoformat(), now, costs)
    assert result["status"] != "PROVE_EDGE_PASSED"
    if defect in {"zero_pnl", "negative_pnl"}:
        assert not result["criteria"]["net_realized_pnl"]["passed"]
        assert not result["criteria"]["net_expectancy"]["passed"]


def test_partial_closures_and_open_lots_are_not_completed_trades():
    rows = passing_rows()
    rows["position_lots"][0]["remaining_quantity"] = "0.5"
    result = assess(rows, START.isoformat(), NOW, COSTS)
    assert result["criteria"]["eligible_round_trips"]["actual"] == 199
    assert result["status"] != "PROVE_EDGE_PASSED"


def test_threshold_policy_is_frozen():
    policy = VerificationPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.minimum_days = 1


def test_seal_preserves_snapshot_and_permits_development_only(generation):
    start_record = {"started_at": START.isoformat()}
    write_once(generation.directory / "started.json", start_record)
    write_once(generation.directory / "started-sha256.json", {"sha256": digest(canonical(start_record))})
    rows = passing_rows()
    with sqlite3.connect(generation.database) as connection:
        for table, payloads in rows.items():
            for i, payload in enumerate(payloads):
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                values = {"record_id": f"r{i}", "payload": json.dumps(payload), "created_at": START.isoformat()}
                for column in columns - values.keys():
                    values[column] = f"v{i}" if column in {"idempotency_key", "fill_id", "originating_fill_id", "broker_fill_id"} else "completed"
                if "status" in columns:
                    values["status"] = payload["status"]
                names = list(values)
                connection.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(values.values()))
    generation.acquire()
    result = generation.report(seal=True)
    assert result["status"] == "PROVE_EDGE_PASSED"
    assert result["workflow"] == "POST_PROVE_EDGE_DEVELOPMENT"
    sealed = (generation.directory / "sealed-evidence.json").read_bytes()
    with sqlite3.connect(generation.database) as connection:
        connection.execute("DELETE FROM trade_attributions")
    assert generation.report()["status"] == "PROVE_EDGE_PASSED"
    assert (generation.directory / "sealed-evidence.json").read_bytes() == sealed
    with pytest.raises(FileExistsError):
        write_once(generation.directory / "sealed-evidence.json", {})
    generation.release()


async def test_valid_guard_never_makes_database_read_only(generation):
    assert await generation.start()
    with sqlite3.connect(generation.database) as connection:
        connection.execute("INSERT INTO audit_events VALUES ('test', '{}', '2026-01-01')")
    assert generation.check()["integrity_valid"]
    generation.release()


def test_new_static_authority_and_symlinks_are_detected(tree, tmp_path):
    directory = frozen_tree(tree, tmp_path)
    (tree / "tradepulse/schema.sql").write_text("SELECT 1;")
    assert verify_source(tree, directory)["added"] == ["tradepulse/schema.sql"]
    (tree / "tradepulse/redirect.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(VerificationError, match="source_not_regular"):
        verify_source(tree, directory)


async def test_start_timestamp_tampering_is_refused(generation):
    assert await generation.start()
    generation.release()
    (generation.directory / "started.json").write_text('{"started_at":"2000-01-01T00:00:00+00:00"}')
    assert not await generation.start()


def test_modified_thresholds_and_configuration_cannot_be_accepted(generation):
    changed = Verification(replace(generation.settings, risk_profile="aggressive"), "paper-1", root=generation.root)
    assert not changed.check()["integrity_valid"]
    manifest = read_manifest(generation.directory)
    assert manifest["policy"]["thresholds"] == VerificationPolicy().as_dict()


async def test_second_official_process_is_refused(generation):
    assert await generation.start()
    second = Verification(generation.settings, "paper-1", root=generation.root)
    assert not await second.start()
    generation.release()


async def test_invalid_generation_is_a_safe_cli_failure(tmp_path):
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/empty.db"})
    async def forbidden(_):
        pytest.fail("invalid generation must never invoke runtime")
    assert await run_official(settings, "../escape", forbidden) == 1


def test_reconciliation_has_explicit_generation_interface(generation):
    from tradepulse.cli import _build_parser
    args = _build_parser().parse_args(["reconcile", "--verification-generation", "paper-1"])
    assert args.verification_generation == "paper-1"
    assert permit_command(generation.settings, "reconcile", "paper-1")
    assert not permit_command(generation.settings, "reconcile", None)


def test_partial_opening_fills_count_once_and_costs_are_not_omitted():
    rows = passing_rows()
    # Equal quantities group by the original opening intent even if it has
    # two separate fills/lots: they are one completed sample, not two wins.
    rows["fills"][2]["trade_intent_id"] = "intent0"
    rows["settlements"][2]["trade_intent_id"] = "intent0"
    rows["trade_attributions"][1]["opening_trade_intent_id"] = "intent0"
    rows["trade_intents"][0]["filled_quantity"] = "2"
    result = assess(rows, START.isoformat(), NOW, COSTS)
    assert result["criteria"]["eligible_round_trips"]["actual"] == 199
    assert result["criteria"]["net_realized_pnl"]["actual"] != "160"


def test_unknown_reconciliation_and_equity_cannot_pass():
    rows = passing_rows()
    rows["equity_snapshots"] = []
    rows["reconciliation_records"] = []
    result = assess(rows, START.isoformat(), NOW, COSTS)
    assert result["status"] != "PROVE_EDGE_PASSED"
    assert result["criteria"]["maximum_drawdown_pct"]["actual"] is None
    assert result["criteria"]["reconciliation_issues"]["actual"] is None


def test_unverified_forced_integrity_reset_cannot_hide_incident():
    rows = passing_rows()
    rows["audit_events"] = [{"event_id": "forced", "occurred_at": NOW.isoformat(), "details": {"action": "reset_integrity_forced"}}]
    result = assess(rows, START.isoformat(), NOW, COSTS)
    assert result["status"] == "PROVE_EDGE_FAILED_INTEGRITY"
