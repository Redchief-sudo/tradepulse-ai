"""Rev.115: protection runs whenever the process runs; scanning follows the session; restarts never override an operator stop."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from test_cli import _settings, _stub_build_dashboard_server
from test_settlement_engine import _repositories
from tradepulse import cli
from tradepulse.models import AssetClass, SessionState, TradingSession
from tradepulse.risk import save_session

NOW = datetime(2026, 10, 2, 14, tzinfo=UTC)


@pytest.mark.parametrize(("state", "resume", "expected"), [
    (SessionState.DISABLED, False, True), (SessionState.MANUALLY_STOPPED, False, True),
    (SessionState.ACTIVE, True, True), (SessionState.MARKET_CLOSED, True, True),
    (SessionState.MANUALLY_STOPPED, True, False), (SessionState.DISABLED, True, False),
    (SessionState.RISK_STOPPED, True, False), (SessionState.FINANCIAL_INTEGRITY_BLOCKED, True, False),
])
def test_resume_never_overrides_an_operator_or_safety_stop(state, resume, expected):
    assert cli._should_activate(state, resume=resume) is expected


async def _tick(repositories, monkeypatch):
    leg = AsyncMock()
    monkeypatch.setattr(cli, "_run_scan_leg", leg)
    ai = AsyncMock()
    wait = await cli._scan_action(AssetClass.CRYPTO, 600, repositories.trade_intents.database, repositories, ai,
                                  AsyncMock(), AsyncMock(), AsyncMock(), None, AsyncMock(), None)
    return wait, leg, ai


@pytest.mark.parametrize("state", [SessionState.MANUALLY_STOPPED, SessionState.DISABLED,
                                   SessionState.RISK_STOPPED, SessionState.FINANCIAL_INTEGRITY_BLOCKED])
async def test_scan_lane_idles_without_any_work_while_session_inactive(tmp_path, monkeypatch, state):
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession(
        "session", state, False, NOW,
        kill_switch_reset_required=state == SessionState.RISK_STOPPED,
        financial_integrity_manual_reenable_required=state == SessionState.FINANCIAL_INTEGRITY_BLOCKED,
    ))
    wait, leg, ai = await _tick(repositories, monkeypatch)
    assert wait == cli.SCAN_IDLE_POLL_SECONDS
    leg.assert_not_awaited()
    ai.scan_candidates.assert_not_awaited()
    assert await repositories.scan_runs.list_all() == []


async def test_operator_start_resumes_scanning_without_restart(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.MANUALLY_STOPPED, False, NOW))
    _, idle_leg, _ = await _tick(repositories, monkeypatch)
    idle_leg.assert_not_awaited()
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))  # what `start` persists
    wait, leg, _ = await _tick(repositories, monkeypatch)
    leg.assert_awaited_once()
    assert wait == 600


async def test_supervisor_starts_every_lane_including_plain_reconcile(monkeypatch):
    started = []

    async def fake_lane(name, factory, repositories, alerts, shutdown, sleep):
        started.append(name)

    monkeypatch.setattr(cli, "_supervised_lane", fake_lane)
    await cli._run_trading_supervisor(None, None, None, None, None, None, None, None, None, asyncio.Event(), None)
    assert sorted(started) == ["crypto", "equity", "monitor", "option", "reconcile", "settle"]


async def test_refused_activation_still_starts_the_supervisor_and_keeps_the_process_up(tmp_path, monkeypatch):
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    supervisor_calls = []

    async def stub_run_dashboard_server(server, shutdown) -> None:
        await shutdown.wait()

    async def stub_run_start(settings) -> int:
        return 1  # refused: risk stop / integrity block / broker unreachable

    async def stub_supervisor(*args, **kwargs) -> None:
        supervisor_calls.append(args)
        args[9].set()  # shutdown is the 10th positional argument

    monkeypatch.setattr(cli, "_build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr(cli, "_run_dashboard_server", stub_run_dashboard_server)
    monkeypatch.setattr(cli, "_run_start", stub_run_start)
    monkeypatch.setattr(cli, "_run_trading_supervisor", stub_supervisor)

    assert await cli._run_application(settings, 8127, False) == 0
    assert len(supervisor_calls) == 1


async def test_resume_does_not_activate_a_stopped_session_but_still_starts_the_supervisor(tmp_path, monkeypatch):
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.MANUALLY_STOPPED, False, NOW))
    settings = _settings(f"sqlite:///{tmp_path}/test.db")
    supervisor_calls = []

    async def stub_run_dashboard_server(server, shutdown) -> None:
        await shutdown.wait()

    async def stub_run_start(settings) -> int:
        raise AssertionError("--resume must never activate an operator-stopped session")

    async def stub_supervisor(*args, **kwargs) -> None:
        supervisor_calls.append(args)
        args[9].set()

    monkeypatch.setattr(cli, "_build_dashboard_server", _stub_build_dashboard_server)
    monkeypatch.setattr(cli, "_run_dashboard_server", stub_run_dashboard_server)
    monkeypatch.setattr(cli, "_run_start", stub_run_start)
    monkeypatch.setattr(cli, "_run_trading_supervisor", stub_supervisor)

    assert await cli._run_application(settings, 8128, False, resume=True) == 0
    assert len(supervisor_calls) == 1
