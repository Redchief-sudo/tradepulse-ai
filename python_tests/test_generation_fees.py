"""Exact account-level fee authority, independent of trade-level models."""
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.persistence.database import SCHEMA
from tradepulse.persistence.codec import decode_payload
from tradepulse.reconciliation.generation_fees import persist_generation_fees, validate_generation_fee
from tradepulse.verification.evidence import observed_generation_result

NOW = datetime(2026, 9, 22, tzinfo=UTC)


def receipt(identifier='cash-1', **fields):
    return {'id': identifier, 'activity_type': 'CFEE', 'description': 'Coin Pair Transaction Fee (USD)',
            'currency': 'USD', 'status': 'executed', 'net_amount': '-1.2345',
            'created_at': '2026-09-21T17:00:00-07:00', **fields}


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def membership(raw, status='in_generation'):
    return {'checkpoint': None, 'classifications': {row['id']: status for row in raw}, 'population_hash': 'proof'}


def payloads(db, table):
    return [decode_payload(row['payload']) for row in db.execute('SELECT payload FROM ' + table)]


def test_unlinked_fee_once_exact_cash_no_invented_asset_or_trade(db):
    raw = receipt()
    for _ in range(3):
        assert persist_generation_fees(db, [raw], membership([raw]), now=NOW) == {'cash-1'}
    fees = payloads(db, 'reconciliation_records')
    cash = payloads(db, 'cash_ledger')
    assert len(fees) == len(cash) == 1
    assert fees[0]['actual']['fee_classification'] == 'unallocated_account_fee'
    assert fees[0]['actual']['authoritative_relationship'] == {}
    assert fees[0]['actual']['activity'] == raw
    assert cash[0]['amount'] == '-1.2345'
    assert cash[0]['occurred_at'] == '2026-09-22T00:00:00+00:00'
    assert payloads(db, 'pnl_records') == payloads(db, 'trade_attributions') == []
    assert validate_generation_fee(fees[0], cash[0], raw=raw)['amount'] == Decimal('-1.2345')


def test_receipt_conflict_and_missing_cash_fail_closed(db):
    raw = receipt()
    persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    changed = receipt(net_amount='-2')
    with pytest.raises(ValueError, match='MISMATCH'):
        persist_generation_fees(db, [changed], membership([changed]), now=NOW)
    db.execute('DELETE FROM cash_ledger')
    with pytest.raises(ValueError, match='CASH_MISSING'):
        persist_generation_fees(db, [raw], membership([raw]), now=NOW)


@pytest.mark.parametrize('stamp', [None, '2026-09-22T00:00:00', 'not-a-date'])
def test_mandatory_fee_timestamp_rejected(db, stamp):
    raw = receipt(created_at=stamp)
    with pytest.raises((ValueError, TypeError)):
        persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    assert payloads(db, 'cash_ledger') == []


def test_pre_generation_fee_excluded_late_fee_included(db):
    old, late = receipt('old'), receipt('late')
    classification = {'checkpoint': None, 'classifications': {
        'old': 'pre_generation', 'late': 'late_arriving_in_generation'}}
    assert persist_generation_fees(db, [old, late], classification, now=NOW) == {'late'}
    assert payloads(db, 'cash_ledger')[0]['entry_id'] == 'broker:fee:late'


def test_observed_cash_fee_deducted_once_and_missing_receipt_is_not_zero(db):
    raw = receipt()
    persist_generation_fees(db, [raw], membership([raw]), now=NOW)
    records = payloads(db, 'reconciliation_records')
    records.append({'record_id': 'asset_fee_population:test', 'actual': {'activities': [raw]}})
    rows = {'reconciliation_records': records, 'trade_attributions': [{'realized_pnl': '10'}]}
    cash = {row['entry_id']: row for row in payloads(db, 'cash_ledger')}
    observed, bridge, errors = observed_generation_result(rows, {}, {}, set(), {}, cash, {})
    assert errors == []
    assert observed == Decimal('8.7655')
    assert bridge['unallocated_account_fees'] == '1.2345'
    assert bridge['modeled_overlay_deducted'] is False
    rows['reconciliation_records'].pop(0)
    assert observed_generation_result(rows, {}, {}, set(), {}, cash, {})[0] is None


def test_modeled_overlay_and_observed_actual_expense_are_separate():
    from python_tests.test_paper_verification import passing_rows, START, NOW as ASSESSED
    from tradepulse.verification.evidence import assess

    rows = passing_rows(count=1, wins=1, opening_fee='0.50')
    result = assess(rows, START.isoformat(), ASSESSED, {'fee_bps': '25', 'slippage_bps': '15'})
    assert result['errors'] == []
    assert Decimal(result['modeled_trade_net']) == Decimal('2') - Decimal('202') * Decimal('0.004')
    assert Decimal(result['observed_generation_net']) == Decimal('1.50')
    assert result['criteria']['win_rate_pct']['actual'] == '100'
    assert result['observed_generation_bridge']['modeled_overlay_deducted'] is False
    assert Decimal(result['accounting_breakdown'][0]['modeled_cost_overlay']) == Decimal('0.808')


def test_native_fee_quote_currency_usd_does_not_create_cash_debit(db):
    raw = receipt('native', description='Coin Pair Transaction Fee (Non USD)',
                  symbol='BTCUSD', qty='-0.0001', net_amount='0')
    assert persist_generation_fees(db, [raw], membership([raw]), now=NOW) == set()
    assert payloads(db, 'cash_ledger') == []


def test_observed_equity_normalizes_crypto_receipt_and_excludes_opening_gain():
    from tradepulse.verification.evidence import observed_generation_equity
    from tradepulse.verification.integrity import canonical, digest

    checkpoint = {'opened_at': NOW.isoformat(), 'account_identity_digest': digest(canonical({
        'account_id': 'sanitized', 'account_number': None})),
        'positions': [{'asset_class': 'crypto', 'symbol': 'BTC/USD', 'qty': '2', 'market_value': '200'}]}
    account = {'received_at': NOW.isoformat(), 'account_id': 'sanitized', 'account_number': None,
               'cash': '1000', 'equity': '1440', 'raw': {'id': 'sanitized', 'cash': '1000', 'equity': '1440'}}
    position = {'asset_class': 'crypto', 'symbol': 'BTC/USD', 'received_at': NOW.isoformat(),
                'qty': '4', 'market_value': '440',
                'raw': {'asset_class': 'crypto', 'symbol': 'BTCUSD', 'qty': '4', 'market_value': '440'}}
    snapshot = {'as_of': NOW.isoformat(), 'total_equity': '1440',
                'reconciliation_results': {'broker_observation': {'account': account, 'positions': [position]}}}
    rows = {'cash_ledger': [], 'reconciliation_records': []}
    # The opening two coins gained 20; only generation inventory performance
    # belongs in the observed generation curve.
    assert observed_generation_equity(snapshot, rows, checkpoint) == Decimal('1420')
    position['received_at'] = '2026-09-21T00:00:00+00:00'
    with pytest.raises(ValueError, match='position_receipt_mismatch'):
        observed_generation_equity(snapshot, rows, checkpoint)
    position['received_at'] = NOW.isoformat()
    position['raw']['asset_class'] = 'us_equity'
    with pytest.raises(ValueError, match='position_receipt_mismatch'):
        observed_generation_equity(snapshot, rows, checkpoint)
