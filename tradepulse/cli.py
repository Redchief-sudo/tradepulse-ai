"""CLI entrypoint -- the sole way into this runtime, invoked externally on a
schedule (cron, systemd timer, etc.). This process does not run its own
internal scheduling loop: each invocation performs exactly one scan cycle
and exits, matching the CLI-driven, cron-external architecture decided for
this system (see docs/). Point cron at it, e.g.:

    */15 9-16 * * 1-5  cd /path/to/repo && .venv/bin/tradepulse scan >> /var/log/tradepulse.log 2>&1

Overlapping invocations are the operator's responsibility to prevent (e.g.
via cron's own overlap handling, or `flock`) -- this process does not take
an inter-process lock.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaClient
from tradepulse.config import Settings, SettingsError, risk_limits_for_profile
from tradepulse.config.logging import configure_logging
from tradepulse.execution import ExecutionGateway
from tradepulse.models import ExecutionMode
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.providers import AlpacaMarketDataProvider, AnthropicAIProvider
from tradepulse.scanner import run_scan_cycle
from tradepulse.settlement import SettlementProcessor
from tradepulse.strategy import load_executable_universe

logger = logging.getLogger(__name__)


def _require_scan_credentials(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", settings.alpaca_api_key),
            ("ALPACA_API_SECRET", settings.alpaca_api_secret),
            ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        )
        if not value
    ]
    if missing:
        raise SettingsError(f"scan requires {', '.join(missing)} to be set")


async def _run_scan(settings: Settings) -> int:
    _require_scan_credentials(settings)
    assert settings.alpaca_api_key and settings.alpaca_api_secret and settings.anthropic_api_key

    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)

    broker = AlpacaClient(settings.alpaca_api_key, settings.alpaca_api_secret, settings.execution_mode, settings.broker_timeout_seconds)
    ai_provider = AnthropicAIProvider(
        settings.anthropic_api_key, settings.anthropic_model, settings.ai_timeout_seconds, settings.anthropic_base_url
    )
    try:
        market_data = AlpacaMarketDataProvider(broker)
        alerts = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
        settlement = SettlementProcessor(repositories, alerts)
        risk_limits = risk_limits_for_profile(settings.risk_profile)
        gateway = ExecutionGateway(
            repositories, broker, market_data, settlement, alerts, risk_limits, ExecutionMode(settings.execution_mode)
        )
        universe = load_executable_universe(settings)

        summary = await run_scan_cycle(repositories, ai_provider, market_data, broker, gateway, universe, risk_limits)
    finally:
        await broker.aclose()
        await ai_provider.aclose()

    logger.info(
        "scan_cycle_finished",
        extra={
            "event": "scan_cycle_finished",
            "scan_generation": summary.scan_run_id,
            "status": summary.status.value,
            "candidates_discovered": summary.candidates_discovered,
            "candidates_approved": summary.candidates_approved,
            "orders_submitted": summary.orders_submitted,
        },
    )
    if summary.error:
        logger.error("scan_cycle_failed", extra={"event": "scan_cycle_failed", "error": summary.error})
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradepulse", description="TradePulse AI trading runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="run one AI-driven scan cycle and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"tradepulse: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings.log_level)

    if args.command == "scan":
        try:
            return asyncio.run(_run_scan(settings))
        except SettingsError as exc:
            print(f"tradepulse: {exc}", file=sys.stderr)
            return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
