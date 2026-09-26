"""Reset must prove current accounting, preserve incidents, and fence new evidence.

All databases and broker observations in these tests are isolated fixtures.
"""
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from tradepulse import session_commands
from tradepulse.config import Settings
from tradepulse.models import (
    CashLedgerEntry,
    IntegrityHold,
    IntegrityHoldType,
    ReconciliationOutcome,
    ReconciliationRecord,
    SessionState,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories
from tradepulse.risk import load_session, save_session

INCIDENT_AT = datetime(2026, 9, 22, 13, 50, 57, tzinfo=UTC)
BROKER_URL = "https://paper-api.alpaca.markets/v2"


async def blocked_database(tmp_path):
    settings = Settings.from_env({
        "ALPACA_API_KEY": "fixture-key",
        "ALPACA_API_SECRET": "fixture-secret",
        "TRADEPULSE_DATABASE_URL": f"sqlite:///{tmp_path}/reset-proof.db",
    })
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    await save_session(repositories, TradingSession(
        "session", SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, INCIDENT_AT,
        financial_integrity_reason="Mandatory accounting invariant mismatch",
        financial_integrity_manual_reenable_required=True,
    ))
    historical = ReconciliationRecord(
        "equity:historical-incident", "equity", "broker_equity",
        ReconciliationOutcome.UNRESOLVED_MISMATCH,
        expected={"total_equity": "99999.99"},
        actual={"cash": "100000", "difference": "-0.01"},
        occurred_at=INCIDENT_AT,
    )
    await repositories.reconciliation_records.create_once(historical.record_id, historical)
    return settings, repositories


def mock_balanced_broker(router, *, account_changes=None):
    account = {
        "id": "fixture-paper-account", "account_number": "fixture-account-number",
        "equity": "100000", "last_equity": "100000", "cash": "100000",
        "buying_power": "100000", "portfolio_value": "100000",
        "long_market_value": "0", "short_market_value": "0",
    }
    account.update(account_changes or {})
    route = router.get(f"{BROKER_URL}/account").mock(return_value=httpx.Response(200, json=account))
    router.get(f"{BROKER_URL}/positions").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{BROKER_URL}/account/activities").mock(return_value=httpx.Response(200, json=[]))
    return route


async def assert_still_blocked(repositories):
    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert session.financial_integrity_manual_reenable_required is True
    assert session.trading_active is False
    assert not any(row["payload"].get("details", {}).get("action") == "reset_integrity"
                   for row in await repositories.audit_events.list_all())


@pytest.mark.parametrize("account_changes", [
    {"equity": "99999.99", "portfolio_value": "99999.99"},
    {"long_market_value": None},
])
async def test_clean_position_summary_cannot_clear_unverified_account_equity(tmp_path, account_changes):
    settings, repositories = await blocked_database(tmp_path)
    with respx.mock(assert_all_called=False) as router:
        account_route = mock_balanced_broker(router, account_changes=account_changes)
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
        assert account_route.called  # positions and fills alone cannot verify this incident
    await assert_still_blocked(repositories)


async def test_unavailable_fresh_account_observation_preserves_latch(tmp_path):
    settings, repositories = await blocked_database(tmp_path)
    with respx.mock(assert_all_called=False) as router:
        account_route = mock_balanced_broker(router)
        account_route.mock(side_effect=httpx.ConnectError("fixture broker unavailable"))
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
        assert account_route.called
    await assert_still_blocked(repositories)


async def test_authoritatively_repaired_history_allows_deliberate_reset(tmp_path):
    settings, repositories = await blocked_database(tmp_path)
    incident = await repositories.reconciliation_records.get("equity:historical-incident")
    with respx.mock(assert_all_called=False) as router:
        account_route = mock_balanced_broker(router)
        assert await session_commands.run_reset_integrity(settings, force=False) == 0
        assert account_route.called
        assert not any(call.request.method == "POST" for call in router.calls)
    session = await load_session(repositories)
    assert session.state == SessionState.MANUALLY_STOPPED
    assert session.financial_integrity_manual_reenable_required is False
    assert session.trading_active is False  # deliberate start remains a separate operation
    assert await repositories.reconciliation_records.get("equity:historical-incident") == incident
    observations = [row["payload"] for row in await repositories.reconciliation_records.list_all()
                    if row["payload"]["reconciliation_type"] == "equity"]
    assert any(row["outcome"] == "matched" and row["occurred_at"] != INCIDENT_AT.isoformat()
               for row in observations)


async def test_reconciled_epoch_label_without_checkpoint_receipt_cannot_clear_latch(tmp_path):
    settings, repositories = await blocked_database(tmp_path)
    epoch = {
        "accounting_epoch_id": "unproven-epoch", "canonical_asset_key": "equity:alpaca:AAPL",
        "fee_accounting_status": "reconciled_net", "checkpoint_id": "missing-receipt",
        "checkpoint_version": 1, "population_proof_id": "missing-population",
        "fill_ids": [], "trade_intent_ids": [], "superseded_checkpoint_ids": [],
    }
    await repositories.accounting_epochs.create_once("unproven-epoch", epoch, status="reconciled_net")
    with respx.mock(assert_all_called=False) as router:
        mock_balanced_broker(router)
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
    await assert_still_blocked(repositories)
    assert (await repositories.accounting_epochs.get("unproven-epoch"))["payload"] == epoch


@pytest.mark.parametrize("new_evidence", ["integrity_hold", "cash_movement"])
async def test_reset_refuses_evidence_committed_after_verification(tmp_path, monkeypatch, new_evidence):
    settings, repositories = await blocked_database(tmp_path)
    original_transition = session_commands.transition_session
    inserted = False

    async def commit_intervening_evidence(repositories_arg, decide, **kwargs):
        nonlocal inserted
        inserted = True
        if new_evidence == "integrity_hold":
            hold = IntegrityHold(
                "new-disputed-order", "new-intent", IntegrityHoldType.FILL_QUANTITY_DISPUTED,
                "new broker quantity dispute after reset verification", datetime.now(UTC),
            )
            await repositories.integrity_holds.create_once(hold.broker_order_id, hold, status=hold.hold_type.value)
        else:
            entry = CashLedgerEntry(
                "new-cash-evidence", "new-cash-evidence", Decimal(-1), "USD", datetime.now(UTC),
                "accounting evidence committed after reset verification",
            )
            await repositories.cash_ledger.create_once(entry.entry_id, entry, unique_value=entry.idempotency_key)
        return await original_transition(repositories_arg, decide, **kwargs)

    monkeypatch.setattr(session_commands, "transition_session", commit_intervening_evidence)
    with respx.mock(assert_all_called=False) as router:
        mock_balanced_broker(router)
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
    assert inserted
    await assert_still_blocked(repositories)
    if new_evidence == "integrity_hold":
        assert await repositories.integrity_holds.get("new-disputed-order") is not None
    else:
        assert await repositories.cash_ledger.get("new-cash-evidence") is not None


async def test_force_cannot_bypass_independent_proof(tmp_path):
    settings, repositories = await blocked_database(tmp_path)
    before = await repositories.trading_sessions.get('session')
    with respx.mock:
        assert await session_commands.run_reset_integrity(settings, force=True) == 1
    assert await repositories.trading_sessions.get('session') == before


async def test_unbound_cash_history_requires_authoritative_opening_evidence(tmp_path):
    settings, repositories = await blocked_database(tmp_path)
    entry = CashLedgerEntry('historical-cash', 'historical-cash', Decimal(100000), 'USD', INCIDENT_AT, 'history')
    await repositories.cash_ledger.create_once(entry.entry_id, entry, unique_value=entry.idempotency_key)
    with respx.mock(assert_all_called=False) as router:
        mock_balanced_broker(router)
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
    await assert_still_blocked(repositories)


async def test_reset_fences_evidence_changed_during_account_observation(tmp_path, monkeypatch):
    settings, repositories = await blocked_database(tmp_path)
    original = session_commands.AlpacaClient.get_account

    async def changed_account(broker):
        account = await original(broker)
        entry = CashLedgerEntry('concurrent-fee', 'concurrent-fee', Decimal(-1), 'USD', datetime.now(UTC), 'new fee')
        await repositories.cash_ledger.create_once(entry.entry_id, entry, unique_value=entry.idempotency_key)
        return account

    monkeypatch.setattr(session_commands.AlpacaClient, 'get_account', changed_account)
    with respx.mock(assert_all_called=False) as router:
        mock_balanced_broker(router)
        assert await session_commands.run_reset_integrity(settings, force=False) == 1
    await assert_still_blocked(repositories)
    observations = await repositories.reconciliation_records.list_all()
    assert not any(row['payload']['outcome'] == 'matched' and row['payload']['reconciliation_type'] == 'equity'
                   for row in observations)


@pytest.mark.parametrize('change', ['none', 'cash', 'account_identity'])
async def test_bound_reset_requires_same_account_and_opening_cash_conservation(tmp_path, change):
    from python_tests.opening_fixtures import OpeningBroker
    from tradepulse.verification.service import freeze

    root = tmp_path / 'source'
    (root / 'tradepulse').mkdir(parents=True)
    (root / 'tradepulse' / 'runtime.py').write_text('VALUE = 1\n')
    settings = Settings.from_env({'ALPACA_API_KEY': 'fixture-key', 'ALPACA_API_SECRET': 'fixture-secret',
                                 'TRADEPULSE_DATABASE_URL': f'sqlite:///{tmp_path}/bound.db'})
    database = AsyncSQLiteDatabase(settings.database_url)
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    await freeze(settings, 'soak-reset', {'fee_bps': '25', 'slippage_bps': '15'}, root, broker=OpeningBroker())
    await save_session(repositories, TradingSession(
        'session', SessionState.FINANCIAL_INTEGRITY_BLOCKED, False, INCIDENT_AT,
        financial_integrity_reason='historical mismatch', financial_integrity_manual_reenable_required=True,
    ))
    changes = {'id': 'sanitized-account-id', 'account_number': 'sanitized-account-number'}
    if change == 'cash':
        changes.update(cash='99999', equity='99999', portfolio_value='99999')
    elif change == 'account_identity':
        changes['id'] = 'different-account'
    with respx.mock(assert_all_called=False) as router:
        mock_balanced_broker(router, account_changes=changes)
        result = await session_commands.run_reset_integrity(settings, force=False)
    if change == 'none':
        assert result == 0
        assert (await load_session(repositories)).state == SessionState.MANUALLY_STOPPED
    else:
        assert result == 1
        await assert_still_blocked(repositories)
