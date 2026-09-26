"""Hashable disposable-run evidence and strict prerequisites for official freeze.

This module never runs a broker session or starts the official clock. Report
construction uses stopped, preserved SQLite evidence; incomplete coverage is
reported explicitly and cannot authorize an official generation.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tradepulse.time import aware_utc

from .evidence import assess, number, snapshot_database
from .integrity import VerificationError, canonical, digest, generation_path, load_json, read_manifest, write_once
from .opening import database_identity, load_opening_checkpoint

SOAK_SCHEMA = "tradepulse-accounting-soak-v1"
AUTHORIZED_COSTS = {"fee_bps": "25", "slippage_bps": "15"}
REQUIRED_LANES = {"equity": 900, "crypto": 600, "option": 1200, "monitor": 120, "settle": 60, "reconcile": 60}
MINIMUM_SECONDS = {1: 12 * 3600, 2: 24 * 3600}
MINIMUM_RESTART_SECONDS = 30 * 60
# Alpaca's server clock and the local receive clock are independent; observed
# broker timestamps lead local receipt by tens of milliseconds. A bounded
# tolerance accepts that jitter while still refusing materially future stamps.
BROKER_CLOCK_SKEW_TOLERANCE_SECONDS = 5


def _startup_record(directory: Path, checkpoint: dict) -> dict | None:
    path = directory / "started.json"
    sidecar = directory / "started-sha256.json"
    if not path.exists() and not sidecar.exists():
        return None
    started = load_json(path)
    if (load_json(sidecar) != {"sha256": digest(canonical(started))}
            or started.get("verification_generation_id") != checkpoint["verification_generation_id"]
            or started.get("generation_opening_checkpoint_id") != checkpoint["checkpoint_id"]):
        raise VerificationError("soak_guarded_start_record_invalid")
    at = aware_utc(started["started_at"], field_name="soak_guarded_started_at")
    if not max(aware_utc(checkpoint["opened_at"]), aware_utc(read_manifest(directory)["created_at"])) <= at <= datetime.now(UTC):
        raise VerificationError("soak_guarded_start_time_invalid")
    return started


def comparable_context(context: dict) -> dict:
    """Distinct databases are required; every other frozen setting is equal."""
    return {key: value for key, value in context.items() if key != "database_path"}


def slippage_evidence(fills: list[dict]) -> dict:
    """Directional realized execution cost, never Fill.slippage's absolute value."""
    sides: dict[str, list[Decimal]] = {"buy": [], "sell": []}
    samples = []
    missing = []
    for fill in fills:
        try:
            reference = number(fill["reference_price"])
            price = number(fill["price"])
            observed = aware_utc(fill["reference_observed_at"], field_name="slippage_reference_observed_at")
            submitted = aware_utc(fill["submitted_at"], field_name="slippage_submitted_at")
            executed = aware_utc(fill["filled_at"], field_name="slippage_filled_at")
            if (not Decimal(0) < reference or price <= 0 or not observed <= submitted <= executed
                    or fill["side"] not in sides):
                raise ValueError("slippage_reference_invalid")
            directional = (price - reference) / reference * 10000
            if fill["side"] == "sell":
                directional = -directional
            sides[fill["side"]].append(directional)
            samples.append({"fill_id": fill["fill_id"], "broker_fill_id": fill["broker_fill_id"],
                            "asset_class": fill["asset"]["asset_class"], "side": fill["side"],
                            "reference_price": str(reference), "fill_price": str(price),
                            "adverse_bps": str(directional)})
        except (KeyError, ValueError, TypeError, ArithmeticError):
            missing.append(fill["fill_id"])
    distributions = {}
    for side, values in sides.items():
        values = sorted(values)
        distributions[side] = {"count": len(values), "maximum_adverse_bps": str(values[-1]) if values else None,
                               "mean_adverse_bps": str(sum(values) / len(values)) if values else None,
                               "p95_adverse_bps": str(values[math.ceil(len(values) * .95) - 1]) if values else None,
                               "above_15_bps": sum(value > 15 for value in values)}
    return {"method": "directional_fill_vs_persisted_pre_submission_reference",
            "samples": samples, "by_side": distributions, "missing_reference_fill_ids": sorted(missing),
            "review_required": any(value > 15 for values in sides.values() for value in values),
            "coverage_complete": bool(samples) and not missing and all(sides.values())}


def _segments(events: list[dict], generation: str) -> tuple[list[dict], list[str]]:
    starts, stops, errors = {}, {}, []
    for event in events:
        if event["event_type"] not in {"verification_runtime_started", "verification_runtime_stopped"}:
            continue
        details = event["details"]
        if details.get("generation") != generation:
            errors.append("runtime_event_generation_mismatch")
            continue
        key = details.get("startup_id")
        if not isinstance(key, str) or not key:
            errors.append("runtime_event_startup_identity_missing")
            continue
        target = starts if event["event_type"] == "verification_runtime_started" else stops
        if key in target:
            errors.append("duplicate_runtime_boundary")
        target[key] = event
    result = []
    if starts.keys() != stops.keys():
        errors.append("runtime_shutdown_evidence_incomplete")
    for key in starts.keys() & stops.keys():
        start = aware_utc(starts[key]["occurred_at"])
        end = aware_utc(stops[key]["occurred_at"])
        if not start < end <= datetime.now(UTC):
            errors.append("runtime_boundary_invalid")
        result.append({"startup_id": key, "started_at": start.isoformat(), "ended_at": end.isoformat(),
                       "duration_seconds": (end - start).total_seconds(),
                       "first_start": starts[key]["details"].get("first_start") is True})
    result.sort(key=lambda segment: segment["started_at"])
    for previous, current in zip(result, result[1:]):
        if aware_utc(previous["ended_at"]) >= aware_utc(current["started_at"]):
            errors.append("overlapping_runtime_segments")
    if result and (not result[0]["first_start"] or any(row["first_start"] for row in result[1:])):
        errors.append("runtime_first_start_evidence_invalid")
    return result, errors


def _lane_evidence(events: list[dict], segments: list[dict]) -> dict:
    result = {}
    for lane, interval in REQUIRED_LANES.items():
        stamps = sorted(aware_utc(event["occurred_at"]) for event in events
                        if event["event_type"] == "verification_lane_cycle" and event["details"].get("lane") == lane)
        all_gaps = []
        covered = bool(segments)
        for segment in segments:
            start, end = aware_utc(segment["started_at"]), aware_utc(segment["ended_at"])
            inside = [stamp for stamp in stamps if start <= stamp <= end]
            # A bounded extra cycle budget allows real work to finish; a lane
            # silent for more than two schedules plus two minutes is unproven.
            maximum = 2 * interval + 120
            gaps = [(right - left).total_seconds() for left, right in zip([start, *inside], [*inside, end])]
            all_gaps.extend(gaps)
            covered = covered and bool(inside) and all(gap <= maximum for gap in gaps)
        result[lane] = {"completed_cycles": len(stamps), "maximum_gap_seconds": max(all_gaps) if all_gaps else None,
                        "maximum_permitted_gap_seconds": 2 * interval + 120, "continuous": covered}
    return result


def _complete_market_session(events: list[dict], segments: list[dict], lanes: dict) -> dict:
    clocks = []
    for event in events:
        if event["event_type"] != "verification_broker_clock":
            continue
        raw = event["details"]
        stamps = {name: aware_utc(raw[name], field_name=f"broker_clock_{name}")
                  for name in ("timestamp", "next_open", "next_close", "received_at")}
        skew = (stamps["timestamp"] - stamps["received_at"]).total_seconds()
        if (not isinstance(raw.get("is_open"), bool) or skew > BROKER_CLOCK_SKEW_TOLERANCE_SECONDS
                or stamps["received_at"] != aware_utc(event["occurred_at"])):
            raise VerificationError("invalid_soak_broker_clock")
        clocks.append({**stamps, "is_open": raw["is_open"], "event_id": event["event_id"]})
    maximum_gap = 2 * REQUIRED_LANES["reconcile"] + 120
    for before in clocks:
        if before["is_open"] or before["timestamp"] >= before["next_open"]:
            continue
        opened = before["next_open"]
        closed = before["next_close"]
        if opened >= closed or (opened - before["timestamp"]).total_seconds() > maximum_gap:
            continue
        for segment in segments:
            start, end = aware_utc(segment["started_at"]), aware_utc(segment["ended_at"])
            if not start <= before["timestamp"] < opened < closed <= end:
                continue
            inside = [clock for clock in clocks if start <= clock["timestamp"] and clock["received_at"] <= end]
            active = sorted((clock for clock in inside if clock["is_open"]
                             and opened <= clock["timestamp"] < closed and clock["next_close"] == closed),
                            key=lambda clock: clock["timestamp"])
            after = [clock for clock in inside if not clock["is_open"] and closed <= clock["timestamp"]
                     and (clock["timestamp"] - closed).total_seconds() <= maximum_gap]
            if not active or not after:
                continue
            stamps = [opened, *(clock["timestamp"] for clock in active), closed]
            if any((right - left).total_seconds() > maximum_gap for left, right in zip(stamps, stamps[1:])):
                continue
            return {"complete": lanes.get("crypto", {}).get("continuous") is True,
                    "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
                    "clock_receipt_count": len(clocks), "authority": "persisted_broker_clock_responses"}
    return {"complete": False, "clock_receipt_count": len(clocks), "authority": "persisted_broker_clock_responses"}


def _late_fee_reopenings(rows: dict, supersessions: list[dict]) -> list[dict]:
    """Link a newly observed fee to the exact preserved and replacement proofs.

    A fee may arrive after a checkpoint while still following the opening
    cursor. Such evidence is late to that checkpoint even when its generation
    membership is ``in_generation``.
    """
    records = {row["record_id"]: row for row in rows["reconciliation_records"]}
    epochs = {row["accounting_epoch_id"]: row for row in rows["accounting_epochs"]}
    result = []
    for receipt in supersessions:
        prior = receipt["actual"]["superseded_checkpoint"]
        current = epochs.get(prior["accounting_epoch_id"])
        old_record = records.get(prior["checkpoint_id"])
        if (not current or current["fee_accounting_status"] != "reconciled_net" or not old_record
                or old_record["actual"] != prior or prior["checkpoint_id"] not in current["superseded_checkpoint_ids"]):
            continue
        old_population = records[prior["population_proof_id"]]["actual"]["activities"]
        new_population = records[current["population_proof_id"]]["actual"]["activities"]
        old_ids = {raw["id"] for raw in old_population}
        new_fees = sorted(raw["id"] for raw in new_population if raw["id"] not in old_ids
                          and raw["activity_type"] in {"FEE", "CFEE"}
                          and ("generation_fee:" + raw["id"] in records or "asset_fee:" + raw["id"] in records))
        if new_fees:
            result.append({"supersession_record_id": receipt["record_id"], "prior_checkpoint_id": prior["checkpoint_id"],
                           "replacement_checkpoint_id": current["checkpoint_id"], "new_fee_activity_ids": new_fees})
    return result


def analyze_soak_database(database: Path, *, run_number: int) -> dict:
    if run_number not in MINIMUM_SECONDS:
        raise VerificationError("soak_run_number_invalid")
    database = database.resolve()
    rows = snapshot_database(database)
    checkpoint = rows["generation_opening_checkpoint"]
    if checkpoint is None or not checkpoint["verification_generation_id"].startswith("soak-"):
        raise VerificationError("soak_requires_disposable_bound_generation")
    generation = checkpoint["verification_generation_id"]
    directory = generation_path(database.parent / (database.name + ".paper-verification"), generation)
    manifest = read_manifest(directory)
    if manifest["policy"]["costs"] != AUTHORIZED_COSTS:
        raise VerificationError("soak_overlay_not_authorized")
    started = _startup_record(directory, checkpoint)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        from tradepulse.persistence import hydrate
        extra = {}
        for table in ("scan_runs", "opportunities"):
            extra[table] = []
            connection.row_factory = sqlite3.Row
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY record_id"):
                payload = json.loads(row["payload"])
                hydrate(table, payload)
                if table == "scan_runs" and row["status"] != payload["status"]:
                    raise VerificationError("soak_scan_status_mismatch")
                extra[table].append(payload)
        locks = [dict(zip(("lock_key", "owner_token", "acquired_at", "expires_at", "command"), row))
                 for row in connection.execute("SELECT lock_key,owner_token,acquired_at,expires_at,command FROM locks")]
        identity = database_identity(connection)
    finally:
        connection.close()
    segments, boundary_errors = _segments(rows["audit_events"], generation)
    if segments and (started is None or aware_utc(segments[0]["started_at"]) < aware_utc(started["started_at"])):
        boundary_errors.append("runtime_precedes_guarded_start_record")
    lanes = _lane_evidence(rows["audit_events"], segments)
    market_session = _complete_market_session(rows["audit_events"], segments, lanes)
    first = segments[0] if segments else None
    start = aware_utc(first["started_at"]) if first else aware_utc(checkpoint["opened_at"])
    end = aware_utc(segments[-1]["ended_at"]) if segments else start
    assessment = assess(rows, start.isoformat(), end, AUTHORIZED_COSTS)
    slippage = slippage_evidence(rows["fills"])
    all_ids = [row["broker_fill_id"] for row in rows["fills"]]
    fee_records = [row for row in rows["reconciliation_records"] if row["record_id"].startswith("generation_fee:")]
    native_fee_records = [row for row in rows["reconciliation_records"] if row["record_id"].startswith("asset_fee:")]
    fee_cash = [row for row in rows["cash_ledger"] if row["idempotency_key"].startswith("broker:fee:")]
    supersessions = [row for row in rows["reconciliation_records"] if row["record_id"].startswith("epoch_proof_superseded:")]
    late_fee_reopenings = _late_fee_reopenings(rows, supersessions)
    by_class = {}
    intents = {row["trade_intent_id"]: row for row in rows["trade_intents"]}
    for asset_class in ("equity", "option", "crypto"):
        own = [row for row in rows["accounting_epochs"] if row["trade_intent_ids"] and {
            intents[identifier]["asset"]["asset_class"] for identifier in row["trade_intent_ids"] if identifier in intents} == {asset_class}]
        by_class[asset_class] = {"epochs": len(own), "reconciled_epochs": sum(row["fee_accounting_status"] == "reconciled_net" for row in own),
                                 "fills": sum(row["asset"]["asset_class"] == asset_class for row in rows["fills"]),
                                 "completed_round_trips": sum(intents[identifier]["asset"]["asset_class"] == asset_class for identifier in assessment["population"])}
    incidents = [row for row in rows["audit_events"] if row["severity"] in {"error", "critical"}
                 or "integrity" in row["event_type"].lower()]
    failed_scans = [row["scan_run_id"] for row in extra["scan_runs"] if row["status"] in {"failed", "running"}]
    criteria = assessment["criteria"]
    integrity_names = ("reconciliation_issues", "settlement_issues", "integrity_issues", "missing_attributions",
                       "duplicate_attributions", "evidence_errors")
    invariants = {
        "duration_met": bool(first) and first["duration_seconds"] >= MINIMUM_SECONDS[run_number],
        "runtime_boundaries_complete": not boundary_errors and bool(segments) and started is not None,
        "all_lanes_continuous": all(row["continuous"] for row in lanes.values()),
        "restart_recovery_exercised": len(segments) >= 2 and not boundary_errors
                                      and all(segment["duration_seconds"] >= MINIMUM_RESTART_SECONDS for segment in segments[1:])
                                      and all(lane["continuous"] for lane in lanes.values()),
        "complete_equity_session": run_number == 1 or market_session["complete"],
        "no_stuck_locks": not locks,
        "no_failed_or_incomplete_scans": bool(extra["scan_runs"]) and not failed_scans,
        "no_duplicate_fills": bool(all_ids) and None not in all_ids and len(all_ids) == len(set(all_ids)),
        "no_duplicate_cash_effects": len(rows["cash_ledger"]) == len({row["idempotency_key"] for row in rows["cash_ledger"]}),
        "fee_receipts_conserved": bool(fee_records or native_fee_records) and {
            row["record_id"].removeprefix("generation_fee:") for row in fee_records} == {
                row["idempotency_key"].removeprefix("broker:fee:") for row in fee_cash}
                                   and assessment["observed_generation_net"] is not None,
        "late_fee_reopening_exercised": bool(late_fee_reopenings),
        "all_asset_classes_exercised": all(row["completed_round_trips"] > 0 and row["reconciled_epochs"] > 0 for row in by_class.values()),
        "slippage_receipts_complete": slippage["coverage_complete"],
        "slippage_overlay_not_exceeded": slippage["coverage_complete"] and not slippage["review_required"],
        "both_performance_authorities_available": all(assessment[key] is not None
            for key in ("modeled_trade_net", "observed_generation_net")),
        **{name: criteria[name]["passed"] for name in integrity_names},
    }
    return {"generation": generation, "database_identity": identity["database_id"], "run_number": run_number,
            "started_at": start.isoformat(), "ended_at": end.isoformat(), "duration_seconds": (end - start).total_seconds(),
            "required_continuous_seconds": MINIMUM_SECONDS[run_number], "continuous_segments": segments,
            "guarded_start": started, "late_fee_reopenings": late_fee_reopenings,
            "runtime_boundary_errors": sorted(set(boundary_errors)), "lanes": lanes, "market_session": market_session,
            "counts": {"scans": len(extra["scan_runs"]), "opportunities": len(extra["opportunities"]),
                       "intents": len(rows["trade_intents"]), "fills": len(rows["fills"]),
                       "settlements": len(rows["settlements"]), "completed_settlements": sum(row["status"] == "completed" for row in rows["settlements"]),
                       "exits": len({row["closing_fill_id"] for row in rows["trade_attributions"]}),
                       "attributed_closures": len(rows["trade_attributions"]),
                       "fee_receipts": len(fee_records) + len(native_fee_records),
                       "cash_fee_receipts": len(fee_records), "native_asset_fee_receipts": len(native_fee_records),
                       "reconciliation_outcomes": dict(Counter(row["outcome"] for row in rows["reconciliation_records"])),
                       "integrity_incidents": len(incidents), "restart_events": max(0, len(segments) - 1),
                       "checkpoint_supersessions": len(supersessions)},
            "incidents": incidents, "asset_classes": by_class, "slippage": slippage,
            "performance": {key: assessment[key] for key in ("modeled_trade_net", "observed_generation_net", "observed_generation_bridge")},
            "assessment_errors": assessment["errors"], "invariants": invariants,
            "status": "PASSED" if all(invariants.values()) else "NOT_PASSED"}


def _artifact_digests(directory: Path) -> dict:
    return {**{path.name: digest(path.read_bytes()) for path in sorted(directory.glob("*.json"))},
            "binding.json": digest((directory.parent / "binding.json").read_bytes())}


def _preserved_database_digest(database: Path) -> str:
    # The preserved artifact is a single DELETE-journal file. A later WAL or
    # hot rollback journal could contain evidence invisible to its file hash.
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise VerificationError("preserved_soak_database_has_unhashed_journal")
    return digest(database.read_bytes())


def create_soak_report(database: Path, report_path: Path, *, run_number: int) -> dict:
    """Preserve a consistent, stopped DB plus immutable artifacts and publish once."""
    import fcntl

    database, report_path = database.resolve(), report_path.resolve()
    checkpoint = load_opening_checkpoint(database)
    if checkpoint is None:
        raise VerificationError("soak_generation_missing")
    generation = checkpoint["verification_generation_id"]
    store = database.parent / (database.name + ".paper-verification")
    directory = generation_path(store, generation)
    manifest = read_manifest(directory)
    if manifest["policy"]["costs"] != AUTHORIZED_COSTS:
        raise VerificationError("soak_overlay_not_authorized")
    if report_path.exists():
        raise VerificationError("soak_report_already_exists")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    preserved = report_path.parent / (report_path.stem + ".preserved.sqlite")
    with (directory / "process.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise VerificationError("stop_soak_runtime_before_reporting") from exc
        # Recheck after acquiring the same exclusive lease used by the runtime.
        if load_opening_checkpoint(database) != checkpoint or read_manifest(directory) != manifest:
            raise VerificationError("soak_artifacts_changed_before_preservation")
        descriptor = os.open(preserved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(preserved)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise VerificationError("soak_database_preservation_not_self_contained")
        finally:
            destination.close()
            source.close()
        preserved_store = preserved.parent / (preserved.name + ".paper-verification")
        preserved_directory = generation_path(preserved_store, generation)
        for artifact in sorted(directory.glob("*.json")):
            write_once(preserved_directory / artifact.name, load_json(artifact))
        write_once(preserved_store / "binding.json", load_json(store / "binding.json"))
        analysis = analyze_soak_database(preserved, run_number=run_number)
        report = {"schema": SOAK_SCHEMA, "created_at": datetime.now(UTC).isoformat(),
                  "database_path": str(preserved), "database_sha256": _preserved_database_digest(preserved),
                  "manifest_sha256": digest(canonical(manifest)), "checkpoint_sha256": digest(canonical(checkpoint)),
                  "artifacts_sha256": _artifact_digests(preserved_directory),
                  "source_digest": manifest["aggregate_sha256"], "configuration": comparable_context(manifest["context"]),
                  "costs": manifest["policy"]["costs"], "account_identity_digest": checkpoint["account_identity_digest"],
                  "analysis": analysis}
        report["sha256"] = digest(canonical(report))
        write_once(report_path, report)
        return report


def verify_soak_prerequisites(reports, *, source_digest: str, context: dict, costs: dict) -> dict:
    if len(reports) != 2:
        raise VerificationError("official_freeze_requires_two_preserved_passing_soak_reports")
    if costs != AUTHORIZED_COSTS:
        raise VerificationError("official_freeze_overlay_not_authorized")
    results, analyses, identities, generations, accounts, run_numbers = [], [], set(), set(), set(), set()
    for raw_path in reports:
        path = Path(raw_path).expanduser().resolve()
        report = load_json(path)
        body = {key: value for key, value in report.items() if key != "sha256"}
        if report.get("schema") != SOAK_SCHEMA or report.get("sha256") != digest(canonical(body)):
            raise VerificationError("soak_report_digest_invalid")
        created = aware_utc(report["created_at"], field_name="soak_report_created_at")
        if created > datetime.now(UTC):
            raise VerificationError("soak_report_created_in_future")
        if (report["source_digest"] != source_digest or report["configuration"] != comparable_context(context)
                or report["costs"] != costs):
            raise VerificationError("soak_source_configuration_or_overlay_changed_rerun_both_soaks")
        database = Path(report["database_path"]).resolve()
        if not database.is_file() or _preserved_database_digest(database) != report["database_sha256"]:
            raise VerificationError("preserved_soak_database_missing_or_changed")
        checkpoint = load_opening_checkpoint(database)
        if checkpoint is None or digest(canonical(checkpoint)) != report["checkpoint_sha256"]:
            raise VerificationError("preserved_soak_checkpoint_changed")
        directory = generation_path(database.parent / (database.name + ".paper-verification"), checkpoint["verification_generation_id"])
        manifest = read_manifest(directory)
        if digest(canonical(manifest)) != report["manifest_sha256"]:
            raise VerificationError("preserved_soak_manifest_changed")
        if _artifact_digests(directory) != report["artifacts_sha256"]:
            raise VerificationError("preserved_soak_artifacts_changed")
        if (manifest["aggregate_sha256"] != report["source_digest"]
                or comparable_context(manifest["context"]) != report["configuration"]
                or manifest["policy"]["costs"] != report["costs"]
                or checkpoint["account_identity_digest"] != report["account_identity_digest"]):
            raise VerificationError("soak_report_manifest_or_account_binding_invalid")
        analysis = analyze_soak_database(database, run_number=report["analysis"]["run_number"])
        if analysis != report["analysis"] or created < aware_utc(analysis["ended_at"]):
            raise VerificationError("soak_evidence_incomplete_or_failed")
        if _preserved_database_digest(database) != report["database_sha256"] or _artifact_digests(directory) != report["artifacts_sha256"]:
            raise VerificationError("preserved_soak_evidence_changed_during_verification")
        analyses.append(analysis)
        identities.add(analysis["database_identity"])
        generations.add(analysis["generation"])
        accounts.add(report["account_identity_digest"])
        run_numbers.add(analysis["run_number"])
        results.append({"report_path": str(path), "report_sha256": report["sha256"],
                        "database_sha256": report["database_sha256"], "generation": analysis["generation"],
                        "database_identity": analysis["database_identity"]})
    if len(identities) != 2 or len(generations) != 2 or len(accounts) != 1 or run_numbers != {1, 2}:
        raise VerificationError("soak_reports_require_distinct_databases_generations_and_both_run_numbers")
    if any(analysis["status"] != "PASSED" or not all(analysis["invariants"].values()) for analysis in analyses):
        raise VerificationError("soak_evidence_incomplete_or_failed")
    return {"schema": SOAK_SCHEMA, "reports": results, "database_identities": sorted(identities),
            "source_digest": source_digest, "costs": costs, "account_identity_digest": next(iter(accounts))}
