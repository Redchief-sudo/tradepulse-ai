"""Population and cursor evidence define the generation independently of clocks."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import sqlite3

import pytest

from test_accounting_epochs import pagination
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.persistence.database import SCHEMA
from tradepulse.reconciliation.epochs import new_epoch
from tradepulse.reconciliation.membership import classify_population, require_resolved, verify_membership_record

OPENED = datetime(2026, 9, 1, 12, tzinfo=UTC)


def receipt(identifier, at, **extra):
    return {'id': identifier, 'activity_type': 'FEE', 'currency': 'USD', 'status': 'executed',
            'net_amount': '-0.12', 'created_at': at.isoformat(), **extra}


@pytest.fixture
def boundary(monkeypatch):
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    raw = [receipt('opening-last', OPENED - timedelta(days=1))]
    checkpoint = {'verification_generation_id': 'isolated-test-generation', 'checkpoint_id': 'checkpoint-one',
                  'opened_at': OPENED.isoformat(), 'activities': raw, 'positions': [],
                  'opening_activity_cursor': {'kind': 'broker_activity', 'last_activity_id': 'opening-last'},
                  'opening_activity_population_hash': sha256(encode_payload(raw).encode()).hexdigest(),
                  'account_identity_digest': 'account-one'}
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_opening_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_closing_checkpoint', lambda _: None)
    yield connection, checkpoint
    connection.close()


def test_population_membership_and_cursor_control_not_timestamp_or_lexical_id(boundary):
    connection, checkpoint = boundary
    raw = [receipt('z-backfill', OPENED + timedelta(minutes=1)), *checkpoint['activities'],
           receipt('a-forward', OPENED + timedelta(minutes=1))]
    result = classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=1))
    assert result['classifications'] == {'z-backfill': 'unresolved_generation_membership',
                                        'opening-last': 'pre_generation', 'a-forward': 'in_generation'}
    with pytest.raises(ValueError, match='UNRESOLVED_GENERATION_MEMBERSHIP'):
        require_resolved(result)
    assert connection.execute('SELECT count(*) FROM broker_activity_inbox').fetchone()[0] == 3
    record = decode_payload(connection.execute('SELECT payload FROM reconciliation_records').fetchone()[0])
    assert record['outcome'] == 'drift_detected'
    assert verify_membership_record(checkpoint, record) == result['classifications']


def test_authoritative_order_link_proves_late_arriving_membership(boundary):
    connection, checkpoint = boundary
    connection.execute('INSERT INTO trade_intents(record_id,idempotency_key,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       ('intent', 'intent', 'filled', encode_payload({'broker_order_id': 'owned-order'}),
                        OPENED.isoformat(), OPENED.isoformat()))
    raw = [receipt('new-backfill', OPENED + timedelta(minutes=1), order_id='owned-order'), *checkpoint['activities']]
    first = classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=1))
    assert first['classifications']['new-backfill'] == 'late_arriving_in_generation'
    count = connection.execute('SELECT count(*) FROM reconciliation_records').fetchone()[0]
    second = classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=2))
    assert first == second
    assert connection.execute('SELECT count(*) FROM reconciliation_records').fetchone()[0] == count


def test_all_asset_epochs_and_restarts_share_exact_opening_checkpoint(boundary):
    connection, checkpoint = boundary
    epochs = [new_epoch(key, key, OPENED + timedelta(days=i), Decimal(0), [key], connection=connection)
              for i, key in enumerate(('equity', 'option', 'crypto'))]
    restarted = new_epoch('crypto2', 'crypto2', OPENED + timedelta(days=15), Decimal(0), ['crypto2'], connection=connection)
    for epoch in [*epochs, restarted]:
        assert epoch['generation_opening_checkpoint_id'] == checkpoint['checkpoint_id']
        assert epoch['generation_opened_at'] == checkpoint['opened_at']
        assert epoch['opening_activity_cursor'] == checkpoint['opening_activity_cursor']
        assert epoch['opening_activity_population_hash'] == checkpoint['opening_activity_population_hash']
        assert epoch['account_identity_digest'] == checkpoint['account_identity_digest']
        assert epoch['verification_generation_id'] == checkpoint['verification_generation_id']


@pytest.mark.parametrize('stamp', [None, '', '2026-09-02T12:00:00', 'malformed'])
def test_activity_mandatory_timestamp_fails_closed(boundary, stamp):
    connection, checkpoint = boundary
    raw = [*checkpoint['activities'], dict(receipt('invalid', OPENED), created_at=stamp)]
    with pytest.raises(ValueError, match='mandatory aware timestamp invalid'):
        classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=1))


def test_offset_timestamp_corrobates_correct_instant(boundary):
    connection, checkpoint = boundary
    raw = [*checkpoint['activities'], dict(receipt('offset', OPENED), created_at='2026-09-01T05:01:00-07:00')]
    result = classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=1))
    assert result['classifications']['offset'] == 'in_generation'


async def test_late_account_fee_supersedes_equity_checkpoint_exactly_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_settlement_engine import _repositories, _no_op_alerter, asset
    from tradepulse.models import Fill, SettlementEvent, TradeIntent, Side, ExecutionMode, TradeIntentStatus
    from tradepulse.reconciliation.equity_epochs import reconcile_equity_epochs
    from tradepulse.settlement import SettlementProcessor
    repositories = await _repositories(tmp_path)
    opening_history = [receipt('old-fee', OPENED - timedelta(days=2))]
    checkpoint = {'verification_generation_id': 'new-official', 'checkpoint_id': 'new-checkpoint',
                  'opened_at': OPENED.isoformat(), 'activities': opening_history, 'positions': [],
                  'opening_activity_cursor': {'kind': 'broker_activity', 'last_activity_id': 'old-fee'}, 'opening_activity_population_hash': 'opening-hash',
                  'account_identity_digest': 'account-hash'}
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_opening_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_closing_checkpoint', lambda _: None)
    stamp = OPENED + timedelta(minutes=1)
    intent = TradeIntent('entry', 'entry', 'opportunity', asset(), Side.BUY, ExecutionMode.PAPER, 'test', stamp,
        requested_quantity=Decimal(1), status=TradeIntentStatus.FILLED, broker_order_id='order')
    await repositories.trade_intents.create_once('entry', intent, status='filled', unique_value='entry')
    fill = Fill('fill', 'entry', 'order', asset(), Side.BUY, ExecutionMode.PAPER,
                Decimal(1), Decimal(100), Decimal(0), Decimal(0), stamp, broker_fill_id='fill')
    await repositories.fills.create_once('fill', fill, unique_value='fill')
    event = SettlementEvent('fill', 'fill', 'entry', asset(), Side.BUY, ExecutionMode.PAPER,
        fill.quantity, fill.price, stamp, broker_order_id='order', broker_fill_id='fill')
    await repositories.settlements.create_once('fill', event, status='pending', unique_value='fill')
    assert (await SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: stamp).process_pending()).completed == 1
    raw = [*opening_history, {'id': 'fill', 'activity_type': 'FILL', 'symbol': 'AAPL', 'side': 'buy', 'qty': '1',
           'price': '100', 'order_id': 'order', 'transaction_time': stamp.isoformat()}]
    class Broker:
        async def get_activities(self, *, activity_type, page_evidence):
            page_evidence.extend(pagination(raw)['pages'])
            return [SimpleNamespace(raw=r, activity_id=r['id']) for r in raw]
        async def get_positions(self):
            return [SimpleNamespace(asset_class=asset().asset_class, symbol='AAPL', qty=Decimal(1))]
    broker = Broker()
    assert await reconcile_equity_epochs(repositories, broker, now=OPENED, clock=lambda: stamp)
    initial = (await repositories.accounting_epochs.list_all())[0]['payload']
    initial_checkpoint = await repositories.reconciliation_records.get(initial['checkpoint_id'])
    assert initial_checkpoint['payload']['occurred_at'] == stamp.isoformat()
    assert await repositories.cash_ledger.get('broker:fee:old-fee') is None
    raw.append(receipt('late-account-fee', stamp + timedelta(minutes=1)))
    assert await reconcile_equity_epochs(repositories, broker, now=stamp + timedelta(minutes=2))
    after = (await repositories.accounting_epochs.list_all())[0]['payload']
    assert after['fee_accounting_status'] == 'reconciled_net'
    assert after['checkpoint_version'] == initial['checkpoint_version'] + 1
    assert after['superseded_checkpoint_ids'] == [initial['checkpoint_id']]
    assert await repositories.reconciliation_records.get(initial['checkpoint_id']) == initial_checkpoint
    assert await repositories.cash_ledger.get('broker:fee:late-account-fee') is not None
    count = len(await repositories.reconciliation_records.list_all())
    cash_before = await repositories.cash_ledger.list_all()
    assert await reconcile_equity_epochs(repositories, broker, now=stamp + timedelta(minutes=3))
    assert (await repositories.accounting_epochs.list_all())[0]['payload'] == after
    assert len(await repositories.reconciliation_records.list_all()) == count
    assert await repositories.cash_ledger.list_all() == cash_before
    assert len(await repositories.broker_activity_inbox.list_all()) == 3


def test_post_generation_requires_sealed_population_and_cursor(boundary, monkeypatch):
    connection, checkpoint = boundary
    prior = [*checkpoint['activities'], receipt('last-generation', OPENED + timedelta(minutes=1))]
    closing = {'sealed_at': (OPENED + timedelta(hours=1)).isoformat(), 'activities': prior,
               'cursor': {'kind': 'broker_activity', 'last_activity_id': 'last-generation'}, 'population_hash': sha256(encode_payload(prior).encode()).hexdigest()}
    monkeypatch.setattr('tradepulse.verification.opening.load_bound_closing_checkpoint', lambda _: closing)
    raw = [receipt('ambiguous-backfill', OPENED + timedelta(hours=2)), *prior,
           receipt('new-after-seal', OPENED + timedelta(hours=2))]
    result = classify_population(connection, raw, pagination(raw), now=OPENED + timedelta(hours=3))
    assert result['classifications']['new-after-seal'] == 'post_generation'
    assert result['classifications']['ambiguous-backfill'] == 'unresolved_generation_membership'
    assert result['classifications']['last-generation'] == 'in_generation'
    record = decode_payload(connection.execute('SELECT payload FROM reconciliation_records').fetchone()[0])
    assert verify_membership_record(checkpoint, record) == result['classifications']


async def test_old_external_generation_fill_is_not_hidden_by_reconciliation_lookback(tmp_path):
    from unittest.mock import AsyncMock
    from tradepulse.broker import AlpacaActivity
    from tradepulse.models import Side
    from tradepulse.reconciliation.coordinator import _reconcile_fills
    from tradepulse.risk import load_session
    from test_settlement_engine import _repositories, _no_op_alerter

    repositories = await _repositories(tmp_path)
    raw = {'id': 'external', 'activity_type': 'FILL', 'order_id': 'external-order', 'symbol': 'AAPL',
           'side': 'buy', 'qty': '1', 'price': '100', 'transaction_time': OPENED.isoformat()}
    broker = AsyncMock()
    broker.get_activities.return_value = [AlpacaActivity('external', 'FILL', 'AAPL', Side.BUY,
        Decimal(1), Decimal(100), OPENED, raw)]
    checked, missed, recovered = await _reconcile_fills(repositories, broker, AsyncMock(), _no_op_alerter(),
        OPENED + timedelta(days=30), timedelta(days=1),
        generation_membership={'classifications': {'external': 'in_generation'}})
    assert (checked, missed, recovered) == (1, 1, 0)
    broker.get_activities.assert_awaited_once_with(activity_type='FILL', since=None)
    assert 'external' in (await load_session(repositories)).halt_reason


@pytest.mark.parametrize('asset_kind', ['equity', 'option'])
@pytest.mark.parametrize('receipt_kind', ['missing', 'account_fee', 'explicit_zero'])
async def test_noncrypto_sell_requires_receipt_backed_fee_evidence(tmp_path, asset_kind, receipt_kind):
    from types import SimpleNamespace
    from test_settlement_engine import _repositories, _no_op_alerter, asset
    from tradepulse.models import AssetClass, AssetIdentity, Fill, SettlementEvent, TradeIntent, Side, ExecutionMode, TradeIntentStatus
    from tradepulse.reconciliation.equity_epochs import reconcile_equity_epochs
    from tradepulse.settlement import SettlementProcessor

    repositories = await _repositories(tmp_path)
    instrument = asset() if asset_kind == 'equity' else AssetIdentity(
        'AAPL260918C00200000', AssetClass.OPTION, 'alpaca:AAPL260918C00200000',
        contract_multiplier=Decimal(100))
    raw = []
    for i, side in enumerate((Side.BUY, Side.SELL)):
        stamp = OPENED + timedelta(minutes=i)
        identifier = f'fill-{i}'
        intent = TradeIntent(identifier, identifier, identifier, instrument, side, ExecutionMode.PAPER,
            'test', stamp, requested_quantity=Decimal(1), status=TradeIntentStatus.FILLED, broker_order_id=identifier)
        await repositories.trade_intents.create_once(identifier, intent, status='filled', unique_value=identifier)
        source = {'fee_source': 'broker_activity', 'fee_currency': 'USD'} if receipt_kind == 'explicit_zero' else {}
        fill = Fill(identifier, identifier, identifier, instrument, side, ExecutionMode.PAPER,
            Decimal(1), Decimal(100 + i), Decimal(0), Decimal(0), stamp, broker_fill_id=identifier, **source)
        await repositories.fills.create_once(identifier, fill, unique_value=identifier)
        event = SettlementEvent(identifier, identifier, identifier, instrument, side, ExecutionMode.PAPER,
            fill.quantity, fill.price, stamp, broker_order_id=identifier, broker_fill_id=identifier)
        await repositories.settlements.create_once(identifier, event, status='pending', unique_value=identifier)
        source_raw = {'commission': '0', 'fee_currency': 'USD'} if receipt_kind == 'explicit_zero' else {}
        raw.append({'id': identifier, 'activity_type': 'FILL', 'symbol': instrument.symbol, 'side': side.value,
            'qty': '1', 'price': str(fill.price), 'order_id': identifier, 'transaction_time': stamp.isoformat(), **source_raw})
    now = OPENED + timedelta(hours=1)
    assert (await SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: now).process_pending()).completed == 2
    if receipt_kind == 'account_fee':
        raw.append(receipt('account-expense', OPENED + timedelta(minutes=2)))
    class Broker:
        async def get_activities(self, *, activity_type, page_evidence):
            page_evidence.extend(pagination(raw)['pages'])
            return [SimpleNamespace(raw=r, activity_id=r['id']) for r in raw]
        async def get_positions(self):
            return []
    complete = await reconcile_equity_epochs(repositories, Broker(), now=OPENED, clock=lambda: now)
    epoch = (await repositories.accounting_epochs.list_all())[0]['payload']
    assert complete is (receipt_kind != 'missing')
    assert epoch['fee_accounting_status'] == ('fee_pending' if receipt_kind == 'missing' else 'reconciled_net')
    if receipt_kind == 'account_fee':
        assert epoch['cash_fee_amount'] == '0'
        assert epoch['generation_fee_evidence_ids'] == ['account-expense']
        assert (await repositories.cash_ledger.get('broker:fee:account-expense'))['payload']['amount'] == '-0.12'
