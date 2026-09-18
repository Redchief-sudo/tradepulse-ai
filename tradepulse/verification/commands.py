"""Minimal CLI and runtime adapter for the independent verification authority."""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from tradepulse.persistence import AsyncSQLiteDatabase, DatabaseError

from .integrity import VerificationError
from .service import Verification, bound_generation, freeze

logger = logging.getLogger(__name__)


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("verification", help="paper-verification generation integrity and completion")
    parser.add_argument("action", choices=("freeze", "verify", "status"))
    parser.add_argument("--generation", required=True)
    parser.add_argument("--fee-bps", type=str, default=None, help="explicit verification-only fee model, per-side basis points")
    parser.add_argument("--slippage-bps", type=str, default=None, help="explicit verification-only slippage model, per-side basis points")


async def command(settings, args) -> int:
    verification = None
    try:
        verification = await asyncio.to_thread(Verification, settings, args.generation)
        if args.action == "freeze":
            from .evidence import number
            if (args.fee_bps is None) != (args.slippage_bps is None):
                raise VerificationError("supply_both_cost_model_rates_or_neither")
            costs = None if args.fee_bps is None else {"fee_bps": str(number(args.fee_bps)), "slippage_bps": str(number(args.slippage_bps))}
            if costs is not None and any(number(rate) < 0 for rate in costs.values()):
                raise VerificationError("negative_cost_model_rate")
            database = await asyncio.to_thread(AsyncSQLiteDatabase, settings.database_url)
            await database.initialize()
            result = await asyncio.to_thread(freeze, settings, args.generation, costs)
        elif args.action == "verify":
            if args.fee_bps is not None or args.slippage_bps is not None:
                raise VerificationError("cost_model_is_freeze_only")
            result = await asyncio.to_thread(verification.check)
        else:
            if args.fee_bps is not None or args.slippage_bps is not None:
                raise VerificationError("cost_model_is_freeze_only")
            # During a run, only that supervisor may seal after its work drains.
            try:
                await asyncio.to_thread(verification.acquire)
            except VerificationError:
                result = await asyncio.to_thread(verification.report)
            else:
                result = await asyncio.to_thread(verification.report, seal=True)
        logger.info("paper_verification_result", extra={"event": "paper_verification_result", "result": result})
        return 1 if result.get("integrity_valid") is False or result.get("status") == "PROVE_EDGE_FAILED_INTEGRITY" else 0
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, DatabaseError, sqlite3.Error) as exc:
        logger.error("paper_verification_failed", extra={"event": "paper_verification_failed", "reason": str(exc)})
        return 1
    finally:
        if verification is not None:
            await asyncio.to_thread(verification.release)


def permit_command(settings, command_name: str, generation: str | None) -> bool:
    """A bound database cannot silently be used as ordinary unlocked evidence."""
    if command_name in {"verification", "status", "stop", "provenance", "release-manifest"}:
        return True
    try:
        binding = bound_generation(settings)
        if generation is not None and (command_name not in {"run", "reconcile"} or generation != binding):
            raise VerificationError("official_generation_binding_missing_or_mismatched")
        if binding is not None and (command_name not in {"run", "reconcile"} or generation != binding):
            raise VerificationError("bound_database_requires_explicit_official_run")
        return True
    except (OSError, ValueError, KeyError, TypeError, DatabaseError) as exc:
        logger.error("paper_verification_command_refused", extra={"event": "paper_verification_command_refused", "reason": str(exc)})
        return False


async def run_official(settings, generation: str, run) -> int:
    verification = None
    try:
        verification = await asyncio.to_thread(Verification, settings, generation)
        if not await verification.start():
            return 1
        code = await run(verification)
        result = await verification.finish()
        logger.info("paper_verification_result", extra={"event": "paper_verification_result", "result": result})
        return 1 if result["status"] == "PROVE_EDGE_FAILED_INTEGRITY" else code
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, DatabaseError, sqlite3.Error) as exc:
        logger.error("paper_verification_failed", extra={"event": "paper_verification_failed", "reason": str(exc)})
        return 1
    finally:
        if verification is not None:
            await asyncio.to_thread(verification.release)
