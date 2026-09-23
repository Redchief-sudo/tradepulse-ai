#!/usr/bin/env python3
"""Run one disposable paper soak, exercise a restart, and preserve its report.

Run 1 requires at least 12 hours and Run 2 at least 24 hours. The second
report additionally requires a complete broker-clock-evidenced equity session.
This runner never freezes or starts an official prove-edge generation.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path

# Direct script execution retains the installed package as the authority.
from tradepulse.verification.integrity import VerificationError
from tradepulse.verification.soak import MINIMUM_RESTART_SECONDS, MINIMUM_SECONDS, create_soak_report

logger = logging.getLogger("tradepulse.accounting_soak")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--run-number", type=int, choices=(1, 2), required=True)
    result.add_argument("--database", type=Path, required=True, help="new nonexistent disposable SQLite path")
    result.add_argument("--report", type=Path, required=True, help="new report path; private DB backup is preserved beside it")
    result.add_argument("--generation", default=None, help="defaults to soak-accounting-1 or soak-accounting-2")
    result.add_argument("--hours", type=float, default=None, help="may extend the minimum 12/24-hour uninterrupted session")
    result.add_argument("--restart-minutes", type=float, default=30, help="restart exercise duration; minimum 30 minutes")
    result.add_argument("--port", type=int, default=8765)
    return result


def configuration(args) -> tuple[Path, Path, str, float]:
    database, report = args.database.expanduser().resolve(), args.report.expanduser().resolve()
    generation = args.generation or f"soak-accounting-{args.run_number}"
    from tradepulse.verification.integrity import generation_path
    generation_path(database.parent, generation)
    if not generation.startswith("soak-"):
        raise VerificationError("soak_runner_cannot_start_official_generation")
    duration = MINIMUM_SECONDS[args.run_number] if args.hours is None else args.hours * 3600
    if duration < MINIMUM_SECONDS[args.run_number] or not duration < float("inf"):
        raise VerificationError("soak_duration_below_required_minimum_or_invalid")
    if not MINIMUM_RESTART_SECONDS / 60 <= args.restart_minutes < float("inf"):
        raise VerificationError("restart_exercise_requires_at_least_30_minutes")
    if database.exists() or any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", ".paper-verification")):
        raise VerificationError("soak_requires_new_database_never_clean_or_reuse")
    if report.exists() or not 1 <= args.port <= 65535:
        raise VerificationError("soak_report_exists_or_invalid_port")
    return database, report, generation, duration


async def _command(environment, log, *arguments) -> int:
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "tradepulse.cli", *arguments,
                                                  env=environment, stdout=log, stderr=log)
    try:
        return await process.wait()
    finally:
        if process.returncode is None:
            await _graceful_stop(process)


def _runtime_start_count(database: Path) -> int:
    if not database.exists():
        return 0
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return connection.execute("SELECT COUNT(*) FROM audit_events WHERE json_extract(payload,'$.event_type')='verification_runtime_started'").fetchone()[0]
    finally:
        connection.close()


def _private_log(path: Path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w")


async def _graceful_stop(process) -> int:
    if process.returncode is None:
        process.send_signal(signal.SIGINT)
    # A stuck shutdown remains a failure. Never kill an in-flight financial
    # projection and then report a successful restart test.
    try:
        return await asyncio.wait_for(process.wait(), timeout=900)
    except TimeoutError as exc:
        raise VerificationError("soak_shutdown_did_not_drain_process_still_requires_operator_stop") from exc


async def _session(environment, log, database: Path, generation: str, port: int, duration: float) -> int:
    starts_before = await asyncio.to_thread(_runtime_start_count, database)
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "tradepulse.cli", "run", "--no-browser",
        "--port", str(port), "--verification-generation", generation, env=environment, stdout=log, stderr=log)
    stopping = False
    try:
        deadline = asyncio.get_running_loop().time() + 600
        while process.returncode is None and await asyncio.to_thread(_runtime_start_count, database) == starts_before:
            if asyncio.get_running_loop().time() >= deadline:
                raise VerificationError("soak_guarded_startup_timeout")
            await asyncio.sleep(1)
        if process.returncode is not None:
            return process.returncode or 1
        # Wall duration starts after the durable successful guarded startup.
        deadline = asyncio.get_running_loop().time() + duration
        while process.returncode is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(30, max(0, deadline - asyncio.get_running_loop().time())))
        if process.returncode is not None:
            return process.returncode or 1
        stopping = True
        return await _graceful_stop(process)
    finally:
        if process.returncode is None and not stopping:
            await _graceful_stop(process)


async def run(args) -> int:
    database, report, generation, duration = await asyncio.to_thread(configuration, args)
    environment = dict(os.environ)
    environment.update(TRADEPULSE_DATABASE_URL=f"sqlite:///{database}", TRADEPULSE_EXECUTION_MODE="paper",
                       TRADEPULSE_LIVE_TRADING_ENABLED="false")
    await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
    log = await asyncio.to_thread(_private_log, report.parent / (report.stem + ".runtime.log"))
    try:
        code = await _command(environment, log, "verification", "freeze", "--generation", generation,
                              "--fee-bps", "25", "--slippage-bps", "15")
        if code:
            raise VerificationError("soak_freeze_failed_see_private_runtime_log")
        code = await _session(environment, log, database, generation, args.port, duration)
        if code == 0:
            # Standalone reconciliation runs only after the first supervisor
            # releases its exclusive generation lease, then restart that DB.
            code = await _command(environment, log, "reconcile", "--verification-generation", generation)
        if code == 0:
            code = await _session(environment, log, database, generation, args.port, args.restart_minutes * 60)
        reconcile_code = await _command(environment, log, "reconcile", "--verification-generation", generation)
        status_code = await _command(environment, log, "verification", "status", "--generation", generation)
        result = await asyncio.to_thread(create_soak_report, database, report, run_number=args.run_number)
        logger.info("accounting_soak_report", extra={"event": "accounting_soak_report", "report": str(report),
                    "sha256": result["sha256"], "status": result["analysis"]["status"]})
        return 0 if not (code or reconcile_code or status_code) and result["analysis"]["status"] == "PASSED" else 1
    finally:
        await asyncio.to_thread(log.close)


def main(argv=None) -> int:
    from tradepulse.config.logging import configure_logging
    configure_logging("INFO")
    try:
        return asyncio.run(run(parser().parse_args(argv)))
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        logger.error("accounting_soak_failed", extra={"event": "accounting_soak_failed", "reason": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
