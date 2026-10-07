"""Rev.123: every blocked state is explained -- cause, scope, last check and resolution."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.blockers import collect_blockers, format_blockers, last_reconciliation_at
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    AuditEvent,
    ExecutionMode,
    IntegrityHold,
    IntegrityHoldType,
    ReconciliationOutcome,
    ReconciliationRecord,
    SessionState,
    Side,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.persistence.codec import encode_payload
from tradepulse.risk import save_session

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
AAPL = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
BTC = AssetIdentity("BTC/USD", AssetClass.CRYPTO, "alpaca:BTC/USD")
SPY = AssetIdentity("SPY", AssetClass.EQUITY, "alpaca:SPY")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def _intent(repositories, intent_id, asset, status, broker_order_id=None):
    intent = TradeIntent(intent_id, intent_id, intent_id, asset, Side.BUY, ExecutionMode.PAPER, "test", NOW,
                         requested_quantity=Decimal(1), status=status, broker_order_id=broker_order_id)
    await repositories.trade_intents.create_once(intent_id, intent, status=status.value, unique_value=intent_id)


def _epoch(key, status, reason=None):
    def write(connection):
        payload = {"canonical_asset_key": key, "fee_accounting_status": status, "reason": reason,
                   "opened_at": NOW.isoformat()}
        connection.execute("INSERT INTO accounting_epochs(record_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (f"epoch:{key}", status, encode_payload(payload), NOW.isoformat(), NOW.isoformat()))
    return write


async def test_a_healthy_database_reports_nothing_blocking(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    assert await collect_blockers(repositories) == []
    assert "Nothing is blocking trading." in format_blockers([], None, NOW)


async def test_every_blocking_state_is_reported_with_its_resolution(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await save_session(repositories, TradingSession("session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, NOW,
                                                    financial_integrity_reason="Accounting drift for AAPL",
                                                    financial_integrity_manual_reenable_required=True))
    await _intent(repositories, "ti-flight", AAPL, TradeIntentStatus.ACCEPTED, broker_order_id="order-1")
    await _intent(repositories, "ti-unknown", BTC, TradeIntentStatus.SUBMISSION_UNKNOWN)
    await _intent(repositories, "ti-stuck", SPY, TradeIntentStatus.SUBMITTED)
    await _intent(repositories, "ti-done", AAPL, TradeIntentStatus.FILLED, broker_order_id="order-0")
    stuck = AuditEvent("stranded_intent_unresolved:ti-stuck:2026-10-06", "stranded_intent_unresolved", "critical",
                       "unresolved", NOW, entity_type="trade_intent", entity_id="ti-stuck",
                       details={"reason": "STRANDED_ORDER_IDENTITY_MISMATCH"})
    await repositories.audit_events.create_once(stuck.event_id, stuck)
    for order_id, intent_id, hold_type in (("order-1", "ti-flight", IntegrityHoldType.VERIFICATION_PENDING),
                                           ("order-0", "ti-done", IntegrityHoldType.FILL_QUANTITY_DISPUTED)):
        hold = IntegrityHold(order_id, intent_id, hold_type, f"{hold_type.value} reason", NOW)
        await repositories.integrity_holds.create_once(order_id, hold, status=hold_type.value)
    database = repositories.trade_intents.database
    await database.run(_epoch("crypto:default:alpaca:BTC/USD", "fee_pending"), write=True)
    await database.run(_epoch("equity:default:alpaca:SPY", "fee_pending", "CHECKPOINT_POSITION_QUANTITY_MISMATCH"), write=True)
    await database.run(_epoch("equity:default:alpaca:AAPL", "reconciled_net"), write=True)
    checked = ReconciliationRecord("r1", "order", "order-1", ReconciliationOutcome.DRIFT_DETECTED,
                                   expected={}, actual={}, occurred_at=NOW + timedelta(minutes=5))
    await repositories.reconciliation_records.create_once("r1", checked)

    blockers = await collect_blockers(repositories)
    summary = {(b.kind, b.subject, b.automatic) for b in blockers}
    assert summary == {
        ("session_latch", "session", False),
        ("integrity_hold", "AAPL", True),        # verification pending: re-verified automatically
        ("integrity_hold", "AAPL", False),       # proven dispute: operator only
        ("accounting_epoch", "BTC/USD", True),   # waiting on fees
        ("accounting_epoch", "SPY", False),      # finalization failing with a reason
        ("order_in_flight", "AAPL", True),
        ("stranded_intent", "BTC/USD", True),    # SUBMISSION_UNKNOWN: retried by the sweep
        ("stranded_intent", "SPY", False),       # proven unresolvable
    }
    in_flight = next(b for b in blockers if b.kind == "order_in_flight")
    assert in_flight.last_checked == (NOW + timedelta(minutes=5)).isoformat()
    assert "protective exits included" in in_flight.blocks
    btc_epoch = next(b for b in blockers if b.kind == "accounting_epoch" and b.subject == "BTC/USD")
    assert "protective exits still allowed" in btc_epoch.blocks
    spy_epoch = next(b for b in blockers if b.kind == "accounting_epoch" and b.subject == "SPY")
    assert "no orders" in spy_epoch.blocks and "CHECKPOINT_POSITION_QUANTITY_MISMATCH" in spy_epoch.cause
    assert "STRANDED_ORDER_IDENTITY_MISMATCH" in next(b for b in blockers if b.subject == "SPY" and b.kind == "stranded_intent").cause
    assert all(b.resolution for b in blockers)
    report = format_blockers(blockers, await last_reconciliation_at(repositories), NOW)
    assert "8 blocker(s)" in report and "NEEDS OPERATOR" in report and "clears automatically" in report


async def test_status_command_prints_the_blocker_report(tmp_path, capsys) -> None:
    from tradepulse.config import Settings
    from tradepulse.session_commands import run_status

    url = f"sqlite:///{tmp_path}/test.db"
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-unknown", BTC, TradeIntentStatus.SUBMISSION_UNKNOWN)
    settings = Settings.from_env({"TRADEPULSE_DATABASE_URL": url, "TRADEPULSE_EXECUTION_MODE": "paper",
                                  "TRADEPULSE_LIVE_TRADING_ENABLED": "false"})
    assert await run_status(settings) == 0
    out = capsys.readouterr().out
    assert "Session: disabled" in out
    assert "[stranded_intent] BTC/USD: submission_unknown intent ti-unknown has no broker order id" in out
