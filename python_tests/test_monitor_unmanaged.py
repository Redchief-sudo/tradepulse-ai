"""Rev.115 F5: a broker position with no local holding has no stop -- find it first, say so once a day."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_settlement_engine import _repositories
from tradepulse.broker import AlpacaPosition
from tradepulse.config import risk_limits_for_profile
from tradepulse.models import AssetClass, AssetIdentity, Holding, asset_identity_key
from tradepulse.monitor.coordinator import run_position_monitor

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)


def _position(symbol="AAPL", qty="5", price="200"):
    q, p = Decimal(qty), Decimal(price)
    return AlpacaPosition(symbol, AssetClass.EQUITY, q, p, q * p, p, Decimal(0))


async def _run(repositories, broker, alerts, gateway=None):
    return await run_position_monitor(repositories, broker, AsyncMock(), gateway or AsyncMock(), alerts,
                                      risk_limits_for_profile("balanced"), clock=lambda: NOW)


async def test_unmanaged_position_alerts_once_per_day(tmp_path):
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position()]
    first, second = await _run(repositories, broker, alerts), await _run(repositories, broker, alerts)
    assert (first.unmanaged_positions, second.unmanaged_positions) == (1, 1)
    assert alerts.send.await_count == 1 and alerts.send.await_args.args[0] == "critical"
    assert [r["payload"]["event_type"] for r in await repositories.audit_events.list_all()] == ["unmanaged_broker_position"]


async def test_opening_inventory_at_opening_quantity_is_not_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position(qty="5")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 0
    alerts.send.assert_not_awaited()


async def test_opening_inventory_at_a_different_quantity_is_unmanaged(tmp_path, monkeypatch):
    checkpoint = {"positions": [{"asset_class": "equity", "symbol": "AAPL", "qty": "5"}]}
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", lambda _: checkpoint)
    repositories = await _repositories(tmp_path)
    broker, alerts = AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position(qty="8")]
    assert (await _run(repositories, broker, alerts)).unmanaged_positions == 1


async def test_unmanaged_detection_precedes_exit_work(tmp_path):
    """MSFT is held locally and breached (exit work runs); AAPL is unmanaged.
    The AAPL alert must already be sent when the first exit call starts."""
    repositories = await _repositories(tmp_path)
    msft = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    await repositories.holdings.create_once(asset_identity_key(msft), Holding(
        msft, Decimal(5), Decimal(300), NOW, stop_loss=Decimal(250)))
    broker, alerts, gateway = AsyncMock(), AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position("MSFT", "5", "240"), _position()]
    alerts_sent_at_first_exit = []

    async def execute(request):
        alerts_sent_at_first_exit.append(alerts.send.await_count)
        return SimpleNamespace(status="rejected")

    gateway.execute_intent.side_effect = execute
    await _run(repositories, broker, alerts, gateway)
    assert alerts_sent_at_first_exit == [1]


async def _breached_msft_setup(tmp_path):
    repositories = await _repositories(tmp_path)
    msft = AssetIdentity("MSFT", AssetClass.EQUITY, "alpaca:MSFT")
    await repositories.holdings.create_once(asset_identity_key(msft), Holding(
        msft, Decimal(5), Decimal(300), NOW, stop_loss=Decimal(250)))
    broker, alerts, gateway = AsyncMock(), AsyncMock(), AsyncMock()
    broker.get_positions.return_value = [_position("MSFT", "5", "240"), _position()]
    gateway.execute_intent.return_value = SimpleNamespace(status="rejected")
    return repositories, broker, alerts, gateway


async def test_checkpoint_loader_failure_does_not_block_exits(tmp_path, monkeypatch):
    def boom(_):
        raise RuntimeError("generation_binding_mismatch")
    monkeypatch.setattr("tradepulse.verification.opening.load_bound_opening_checkpoint", boom)
    repositories, broker, alerts, gateway = await _breached_msft_setup(tmp_path)
    summary = await _run(repositories, broker, alerts, gateway)
    gateway.execute_intent.assert_awaited()
    assert summary.unmanaged_positions == 1 and alerts.send.await_count == 1


async def test_detection_failure_does_not_block_exits(tmp_path, monkeypatch):
    repositories, broker, alerts, gateway = await _breached_msft_setup(tmp_path)
    monkeypatch.setattr(repositories.audit_events, "create_once", AsyncMock(side_effect=RuntimeError("database is locked")))
    await _run(repositories, broker, alerts, gateway)
    gateway.execute_intent.assert_awaited()
