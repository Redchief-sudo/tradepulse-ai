"""Offline lifecycle, atomicity, and asset-class isolation regressions."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D
from hashlib import sha256

import pytest

from python_tests.test_asset_fees import ASSET, KEY, NOW, four_receipts, population, seeded
from tradepulse.models import AssetClass, AssetIdentity
from tradepulse.persistence import DatabaseError, PersistenceRepositories, hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.reconciliation.epochs import AccountingEpochPending, record_gross_fill, reserve_epoch
from tradepulse.reconciliation.fee_replay import replay_asset_fees


def pagination(raw):
    return {'method': 'resume_boundary_and_full_history_audit', 'complete': True,
            'population_hash': sha256(encode_payload(raw).encode()).hexdigest(),
            'activity_ids': [r['id'] for r in raw], 'pages': [
                {'request': {'page_size': '100', 'direction': 'asc'},
                 'activity_ids': [r['id'] for r in raw], 'terminal': True,
                 'response_hash': sha256(encode_payload(raw).encode()).hexdigest()}]}


async def test_historical_epoch_net_and_idempotent_inbox_cursor(tmp_path):
    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert epoch['fee_accounting_status'] == 'reconciled_net'
    assert D(epoch['gross_filled_quantity']) == D('36.723631330')
    assert D(epoch['net_inventory_quantity']) == D('36.631822251')
    assert D(epoch['asset_fee_quantity']) == D('0.091809079')
    tables = [r.accounting_epochs, r.broker_activity_inbox, r.broker_activity_cursors, r.reconciliation_records]
    before = [await t.list_all() for t in tables]
    assert not await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert [await t.list_all() for t in tables] == before


async def test_no_fee_is_provisional_and_same_asset_serialized_across_restart(tmp_path):
    r = await seeded(tmp_path)
    raw, qty = await population(r, [])
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert (await r.accounting_epochs.list_all())[0]['status'] == 'fee_pending'
    restarted = PersistenceRepositories.create(r.accounting_epochs.database)
    original = hydrate('trade_intents', (await r.trade_intents.get('intent-0'))['payload'])
    new = replace(original, trade_intent_id='later')
    with pytest.raises(AccountingEpochPending):
        await reserve_epoch(restarted, new, qty, protective=False, now=NOW)
    for asset in [AssetIdentity('BTC/USD', AssetClass.CRYPTO, 'alpaca:BTC/USD'),
                  AssetIdentity('AAPL', AssetClass.EQUITY, 'alpaca:AAPL')]:
        await reserve_epoch(restarted, replace(new, asset=asset), D(0), protective=False, now=NOW)
    await reserve_epoch(restarted, new, qty, protective=True, now=NOW)
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert 'later' in epoch['trade_intent_ids']


async def test_conserved_unallocated_cash_receipt_closes_crypto_buy_checkpoint_once(tmp_path):
    r = await seeded(tmp_path)
    raw, qty = await population(r, [])
    raw.append({'id': 'account-fee', 'activity_type': 'FEE', 'currency': 'USD', 'status': 'executed',
                'net_amount': '-1.23', 'created_at': NOW.isoformat()})
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert epoch['fee_accounting_status'] == 'reconciled_net'
    assert epoch['generation_fee_evidence_ids'] == ['account-fee']
    assert epoch['fee_evidence_ids'] == []
    assert epoch['cash_fee_amount'] == '0'
    assert (await r.cash_ledger.get('broker:fee:account-fee'))['payload']['amount'] == '-1.23'
    tables = [r.fills, r.accounting_epochs, r.cash_ledger, r.position_lots, r.trade_attributions, r.pnl_records, r.reconciliation_records]
    before = [await table.list_all() for table in tables]
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert [await table.list_all() for table in tables] == before


async def test_cursor_and_inbox_rollback_with_accounting_writes(tmp_path):
    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    tables = [r.position_lots, r.holdings, r.reconciliation_records, r.accounting_epochs,
              r.broker_activity_inbox, r.broker_activity_cursors]
    before = [await t.list_all() for t in tables]
    await r.holdings.database.run(lambda c: c.execute(
        "CREATE TRIGGER reject_cursor BEFORE INSERT ON broker_activity_cursors "
        "BEGIN SELECT RAISE(ABORT,'cursor rollback'); END"), write=True)
    with pytest.raises(DatabaseError, match='cursor rollback'):
        await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert [await t.list_all() for t in tables] == before


@pytest.mark.parametrize('tamper', ['hash', 'incomplete', 'page_hash'])
async def test_invalid_population_cannot_finalize_or_advance(tmp_path, tamper):
    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    proof = pagination(raw)
    if tamper == 'hash':
        proof['population_hash'] = 'tampered'
    elif tamper == 'incomplete':
        proof['complete'] = False
    else:
        proof['pages'][0]['response_hash'] = 'tampered'
    with pytest.raises(ValueError, match='ACTIVITY_'):
        await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=proof)
    assert not await r.broker_activity_cursors.list_all()
    assert not await r.accounting_epochs.list_all()


async def test_gross_fill_pending_states_and_retry_are_atomic(tmp_path):
    r = await seeded(tmp_path)
    fill = hydrate('fills', (await r.fills.get('fill-0'))['payload'])
    await record_gross_fill(r, fill, now=NOW)
    epoch = (await r.accounting_epochs.list_all())[0]['payload']
    assert epoch['fee_accounting_status'] == 'fee_pending'
    assert epoch['net_inventory_quantity'] is None
    assert D(epoch['gross_filled_quantity']) == fill.quantity
    rows = await r.reconciliation_records.list_all()
    states = [x['payload']['actual']['fee_accounting_status'] for x in rows]
    assert states == ['filled_gross', 'fee_pending']
    await record_gross_fill(r, fill, now=NOW)
    assert await r.reconciliation_records.list_all() == rows


async def test_concurrent_epoch_admission_has_one_winner(tmp_path):
    r = await seeded(tmp_path)
    original = hydrate('trade_intents', (await r.trade_intents.get('intent-0'))['payload'])
    asset = AssetIdentity('BTC/USD', AssetClass.CRYPTO, 'alpaca:BTC/USD')
    outcomes = await asyncio.gather(*[
        reserve_epoch(r, replace(original, asset=asset, trade_intent_id=f'new-{i}'), D(0), protective=False, now=NOW)
        for i in range(2)], return_exceptions=True)
    assert sum(isinstance(result, AccountingEpochPending) for result in outcomes) == 1


async def test_pending_risk_uses_broker_net_quantity_and_equity(tmp_path):
    from types import SimpleNamespace

    from tradepulse.risk.engine import build_portfolio_snapshot
    r = await seeded(tmp_path)
    raw, qty = await population(r, [])
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    broker_position = SimpleNamespace(qty=D('36.631822251'), market_value=D('3663.1822251'))
    snapshot = await build_portfolio_snapshot(r, cash_balance=D(10000), account_equity=D('13663.1822251'),
        broker_prev_close_equity=D(14000), broker_positions={KEY: broker_position}, mark_prices={KEY: D(100)}, now=NOW)
    assert snapshot.holdings_value == D('3663.1822251')
    assert snapshot.total_equity == D('13663.1822251')
    with pytest.raises(ValueError, match='PENDING_CRYPTO_REQUIRES_BROKER_EXPOSURE'):
        await build_portfolio_snapshot(r, cash_balance=D(10000), mark_prices={KEY: D(100)}, now=NOW)


async def test_late_earlier_fee_preserves_original_allocation_receipt(tmp_path):
    from python_tests.test_asset_fees import receipt
    r = await seeded(tmp_path)
    late = receipt('later', '-13.90', '2026-09-03T19:35:15Z')
    earlier = receipt('earlier', '-0.02', '2026-09-03T19:35:14Z')
    raw, qty = await population(r, [late])
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    original = await r.reconciliation_records.get('asset_fee:later')
    raw, qty = await population(r, [earlier, late])
    assert await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert await r.reconciliation_records.get('asset_fee:later') == original
    assert any(row['record_id'].startswith('asset_fee_allocation:') for row in await r.reconciliation_records.list_all())
    before = await r.reconciliation_records.list_all()
    assert not await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    assert await r.reconciliation_records.list_all() == before


async def test_new_receipt_supersedes_checkpoint_and_reconciles_new_version(tmp_path):
    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts()[:2])
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    old = (await r.accounting_epochs.list_all())[0]['payload']
    assert old['fee_accounting_status'] == 'reconciled_net'
    receipt = await r.reconciliation_records.get(old['checkpoint_id'])
    raw, qty = await population(r, four_receipts())
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=pagination(raw))
    new = (await r.accounting_epochs.list_all())[0]['payload']
    assert new['fee_accounting_status'] == 'reconciled_net'
    assert new['checkpoint_version'] == old['checkpoint_version'] + 1
    assert old['checkpoint_id'] in new['superseded_checkpoint_ids']
    assert new['checkpoint_id'] != old['checkpoint_id']
    assert await r.reconciliation_records.get(old['checkpoint_id']) == receipt
    assert any(row['record_id'].startswith('epoch_proof_superseded:') for row in await r.reconciliation_records.list_all())


@pytest.mark.parametrize('legacy_mismatch', [False, True])
async def test_refreshed_page_receipt_versions_checkpoint_and_preserves_history(tmp_path, legacy_mismatch):
    from datetime import timedelta
    from tradepulse.reconciliation.epochs import checkpoint_issues, verify_checkpoint

    r = await seeded(tmp_path)
    raw, qty = await population(r, four_receipts())
    first = pagination(raw)
    first['pages'][0]['received_at'] = NOW.isoformat()
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW, pagination=first)
    old = (await r.accounting_epochs.list_all())[0]['payload']
    receipt = await r.reconciliation_records.get(old['checkpoint_id'])
    refreshed = pagination(raw)
    refreshed['pages'][0]['received_at'] = (NOW + timedelta(minutes=5)).isoformat()
    if legacy_mismatch:
        broken = {**old, 'pagination_proof': refreshed}
        await r.accounting_epochs.update(old['accounting_epoch_id'], broken, status='reconciled_net')
        issues = await r.accounting_epochs.database.run(checkpoint_issues)
        assert issues[old['accounting_epoch_id']] == 'CHECKPOINT_RECEIPT_MISMATCH'
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW + timedelta(minutes=5), pagination=refreshed)
    new = (await r.accounting_epochs.list_all())[0]['payload']
    assert new['checkpoint_version'] == old['checkpoint_version'] + 1
    assert old['checkpoint_id'] in new['superseded_checkpoint_ids']
    assert new['checkpoint_id'] != old['checkpoint_id']
    assert await r.reconciliation_records.get(old['checkpoint_id']) == receipt
    records = {row['record_id']: row['payload'] for row in await r.reconciliation_records.list_all()}
    fills = [row['payload'] for row in await r.fills.list_all()]
    intents = [row['payload'] for row in await r.trade_intents.list_all()]
    verify_checkpoint(new, records, fills, intents)
    verify_checkpoint(old, records, fills, intents)
    assert await r.accounting_epochs.database.run(checkpoint_issues) == {}
    before = await r.reconciliation_records.list_all()
    await replay_asset_fees(r, ASSET, raw, qty, now=NOW + timedelta(minutes=5), pagination=refreshed)
    assert await r.reconciliation_records.list_all() == before


async def test_checkpoint_verifier_refuses_uncovered_execution_population(tmp_path):
    from tradepulse.reconciliation.epochs import checkpoint_issues
    r = await seeded(tmp_path)
    issues = await r.accounting_epochs.database.run(checkpoint_issues)
    assert 'CHECKPOINT_FILL_MEMBERSHIP_MISSING' in issues.values()
    assert 'CHECKPOINT_EPOCH_MISSING' in issues.values()
