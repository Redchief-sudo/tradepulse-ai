"""Immutable cash-fee receipts conserved once at account-generation scope.

These synchronous helpers execute inside the caller's SQLite transaction. An
account fee has no synthetic asset, trade, or PnL projection. Its cash debit and
complete broker receipt are the financial authority; an explicit broker link is
retained without inventing a lot-level allocation.
"""
from decimal import Decimal

from tradepulse.models import CashLedgerEntry, ReconciliationOutcome, ReconciliationRecord
from tradepulse.models.base import decimal_value, require_text
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.settlement.accounting import _persist
from tradepulse.time import aware_utc

ELIGIBLE_MEMBERSHIPS = frozenset({'in_generation', 'late_arriving_in_generation'})


def cash_fee_receipt(raw):
    """Return an exact cash debit, or None for a non-cash-fee activity."""
    if raw.get('activity_type') not in {'FEE', 'CFEE'}:
        return None
    if raw.get('activity_type') == 'CFEE' and raw.get('description') == 'Coin Pair Transaction Fee (Non USD)':
        from .asset_fees import parse_asset_fee
        parse_asset_fee(raw)  # native receipts can carry USD as their quote currency
        return None
    identifier = require_text(raw.get('id'), 'fee_activity_id')
    if (raw.get('currency') != 'USD' or raw.get('status') != 'executed'
            or isinstance(raw.get('net_amount'), (float, bool))
            or decimal_value(raw.get('qty', '0'), 'cash_fee_quantity') != 0):
        raise ValueError('GENERATION_FEE_UNSUPPORTED_RECEIPT:' + identifier)
    amount = decimal_value(raw.get('net_amount'), 'cash_fee_amount')
    if amount >= 0:
        raise ValueError('GENERATION_FEE_NOT_A_DEBIT:' + identifier)
    occurred_at = aware_utc(raw.get('created_at'), field_name='fee_created_at')
    relation = {key: require_text(raw[key], 'fee_' + key)
                for key in ('fill_id', 'order_id', 'asset_id', 'symbol') if raw.get(key) is not None}
    classification = ('authoritative_fill_fee' if 'fill_id' in relation else
                      'authoritative_order_fee' if 'order_id' in relation else
                      'authoritative_asset_fee' if relation else 'unallocated_account_fee')
    return {'activity_id': identifier, 'amount': amount, 'occurred_at': occurred_at,
            'relationship': relation, 'fee_classification': classification}


def fee_cash_entry(raw):
    fee = cash_fee_receipt(raw)
    if fee is None:
        raise ValueError('GENERATION_FEE_NOT_CASH')
    identifier = 'broker:fee:' + fee['activity_id']
    return CashLedgerEntry(identifier, identifier, fee['amount'], 'USD', fee['occurred_at'],
                          'immutable broker cash fee; generation-accounting-v1')


def _receipt_actual(raw, checkpoint):
    fee = cash_fee_receipt(raw)
    return {'schema_version': 1, 'activity': dict(raw),
            'verification_generation_id': checkpoint['verification_generation_id'] if checkpoint else None,
            'generation_opening_checkpoint_id': checkpoint['checkpoint_id'] if checkpoint else None,
            'fee_classification': fee['fee_classification'], 'authoritative_relationship': fee['relationship'],
            'cash_entry_id': 'broker:fee:' + fee['activity_id'], 'currency': 'USD',
            'cash_debit': str(fee['amount']), 'occurred_at': fee['occurred_at'].isoformat()}


def validate_generation_fee(record, entry, *, raw=None, checkpoint=None):
    """Read-side proof; missing receipts, changed identities, or cash fail closed."""
    actual = record['actual']
    receipt = actual['activity']
    fee = cash_fee_receipt(receipt)
    if fee is None:
        raise ValueError('GENERATION_FEE_INVALID_RECEIPT')
    identifier = 'generation_fee:' + fee['activity_id']
    if (record['record_id'] != identifier or record['subject_id'] != identifier
            or record['reconciliation_type'] != 'asset_fee' or record['outcome'] != 'matched'
            or actual != _receipt_actual(receipt, checkpoint)
            or entry != decode_payload(encode_payload(fee_cash_entry(receipt)))
            or (raw is not None and dict(raw) != receipt)):
        raise ValueError('GENERATION_FEE_RECEIPT_OR_CASH_MISMATCH:' + fee['activity_id'])
    aware_utc(record['occurred_at'], field_name='fee_receipt_recorded_at')
    return fee


def persist_generation_fees(connection, activities, membership, *, now):
    """Record each eligible cash fee once; caller owns atomic commit/rollback."""
    now = aware_utc(now, field_name='fee_capture_time')
    checkpoint = membership['checkpoint']
    classifications = membership['classifications']
    seen = set()
    recorded = set()
    for raw in activities:
        identifier = require_text(raw.get('id'), 'activity_id')
        if identifier in seen:
            raise ValueError('GENERATION_FEE_DUPLICATE_ACTIVITY:' + identifier)
        seen.add(identifier)
        fee = cash_fee_receipt(raw)
        if fee is None:
            continue
        status = classifications.get(identifier)
        if status == 'unresolved_generation_membership' or status is None:
            raise ValueError('GENERATION_FEE_MEMBERSHIP_UNRESOLVED:' + identifier)
        if status not in ELIGIBLE_MEMBERSHIPS:
            continue
        record_id = 'generation_fee:' + identifier
        entry = fee_cash_entry(raw)
        actual = _receipt_actual(raw, checkpoint)
        row = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?', (record_id,)).fetchone()
        if row:
            cash = connection.execute('SELECT payload FROM cash_ledger WHERE record_id=?', (entry.entry_id,)).fetchone()
            if cash is None:
                raise ValueError('GENERATION_FEE_CASH_MISSING:' + identifier)
            validate_generation_fee(decode_payload(row['payload']), decode_payload(cash['payload']),
                                    raw=raw, checkpoint=checkpoint)
        else:
            # No reuse of a legacy/inferred debit: conflicting immutable rows fail
            # atomically, requiring an explicit evidence repair outside the run.
            _persist(connection, 'cash_ledger', entry.entry_id, entry, write=True)
            record = ReconciliationRecord(record_id, 'asset_fee', record_id, ReconciliationOutcome.MATCHED,
                expected={'immutable_fee_receipt': True, 'cash_debit_conserved_once': True},
                actual=actual, occurred_at=now)
            connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                               (record_id, encode_payload(record), now.isoformat()))
        recorded.add(identifier)
    _persist_generation_adjustments(connection, activities, membership, now=now)
    return recorded


def generation_fee_recorded(connection, activity_id):
    """Verify the persisted cash debit and receipt, never an existence-only flag."""
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    record = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?',
                                ('generation_fee:' + activity_id,)).fetchone()
    if record is None:
        return False
    cash = connection.execute('SELECT payload FROM cash_ledger WHERE record_id=?',
                              ('broker:fee:' + activity_id,)).fetchone()
    if cash is None:
        return False
    validate_generation_fee(decode_payload(record['payload']), decode_payload(cash['payload']),
                            checkpoint=load_bound_opening_checkpoint(connection))
    return True


def adjustment_receipt(raw):
    """Only broker-declared transfers and interest establish economic purpose."""
    kind = raw.get('activity_type')
    if kind not in {'CSD', 'CSW', 'INT'}:
        return None
    identifier = require_text(raw.get('id'), 'adjustment_activity_id')
    if (raw.get('currency') != 'USD' or raw.get('status') != 'executed'
            or isinstance(raw.get('net_amount'), (float, bool))
            or decimal_value(raw.get('qty', '0'), 'adjustment_quantity') != 0):
        raise ValueError('GENERATION_ADJUSTMENT_UNSUPPORTED_RECEIPT:' + identifier)
    amount = decimal_value(raw.get('net_amount'), 'adjustment_amount')
    if amount == 0 or (kind == 'CSD' and amount < 0) or (kind == 'CSW' and amount > 0):
        raise ValueError('GENERATION_ADJUSTMENT_DIRECTION_INVALID:' + identifier)
    return {'activity_id': identifier, 'amount': amount,
            'occurred_at': aware_utc(raw.get('created_at'), field_name='adjustment_created_at'),
            'economic_type': 'capital_flow' if kind in {'CSD', 'CSW'} else 'interest_income_or_expense'}


def adjustment_cash_entry(raw):
    adjustment = adjustment_receipt(raw)
    if adjustment is None:
        raise ValueError('GENERATION_ADJUSTMENT_UNCLASSIFIED')
    identifier = 'broker:adjustment:' + adjustment['activity_id']
    return CashLedgerEntry(identifier, identifier, adjustment['amount'], 'USD', adjustment['occurred_at'],
                          'immutable broker cash adjustment; generation-accounting-v1')


def _adjustment_actual(raw, checkpoint):
    adjustment = adjustment_receipt(raw)
    return {'schema_version': 1, 'activity': dict(raw),
            'verification_generation_id': checkpoint['verification_generation_id'] if checkpoint else None,
            'generation_opening_checkpoint_id': checkpoint['checkpoint_id'] if checkpoint else None,
            'economic_type': adjustment['economic_type'], 'cash_amount': str(adjustment['amount']),
            'cash_entry_id': 'broker:adjustment:' + adjustment['activity_id'], 'currency': 'USD',
            'occurred_at': adjustment['occurred_at'].isoformat()}


def validate_generation_adjustment(record, entry, *, raw=None, checkpoint=None):
    receipt = record['actual']['activity']
    adjustment = adjustment_receipt(receipt)
    if adjustment is None:
        raise ValueError('GENERATION_ADJUSTMENT_UNCLASSIFIED')
    identifier = 'generation_adjustment:' + adjustment['activity_id']
    if (record['record_id'] != identifier or record['subject_id'] != identifier
            or record['reconciliation_type'] != 'asset_fee' or record['outcome'] != 'matched'
            or record['actual'] != _adjustment_actual(receipt, checkpoint)
            or entry != decode_payload(encode_payload(adjustment_cash_entry(receipt)))
            or (raw is not None and dict(raw) != receipt)):
        raise ValueError('GENERATION_ADJUSTMENT_RECEIPT_OR_CASH_MISMATCH')
    aware_utc(record['occurred_at'], field_name='adjustment_recorded_at')
    return adjustment


def _persist_generation_adjustments(connection, activities, membership, *, now):
    for raw in activities:
        if membership['classifications'].get(raw['id']) not in ELIGIBLE_MEMBERSHIPS:
            continue
        if raw.get('activity_type') == 'JNLC':
            raise ValueError('GENERATION_ADJUSTMENT_ECONOMIC_PURPOSE_UNRESOLVED:' + raw['id'])
        adjustment = adjustment_receipt(raw)
        if adjustment is None:
            continue
        identifier = 'generation_adjustment:' + raw['id']
        entry = adjustment_cash_entry(raw)
        old = connection.execute('SELECT payload FROM reconciliation_records WHERE record_id=?', (identifier,)).fetchone()
        if old:
            cash = connection.execute('SELECT payload FROM cash_ledger WHERE record_id=?', (entry.entry_id,)).fetchone()
            if cash is None:
                raise ValueError('GENERATION_ADJUSTMENT_CASH_MISSING')
            validate_generation_adjustment(decode_payload(old['payload']), decode_payload(cash['payload']),
                                           raw=raw, checkpoint=membership['checkpoint'])
            continue
        _persist(connection, 'cash_ledger', entry.entry_id, entry, write=True)
        record = ReconciliationRecord(identifier, 'asset_fee', identifier, ReconciliationOutcome.MATCHED,
            expected={'exact_cash_movement_conserved': True, 'economic_purpose_from_broker_type': True},
            actual=_adjustment_actual(raw, membership['checkpoint']), occurred_at=now)
        connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (identifier, encode_payload(record), now.isoformat()))


def generation_adjustment_totals(connection):
    """Return verified signed cash capital flows and recognized interest PnL."""
    from tradepulse.verification.opening import load_bound_opening_checkpoint

    checkpoint = load_bound_opening_checkpoint(connection)
    totals = {'capital_flows': Decimal(0), 'profit_adjustments': Decimal(0)}
    for row in connection.execute("SELECT payload FROM reconciliation_records WHERE record_id LIKE 'generation_adjustment:%'"):
        record = decode_payload(row['payload'])
        cash = connection.execute('SELECT payload FROM cash_ledger WHERE record_id=?',
                                  (record['actual']['cash_entry_id'],)).fetchone()
        if cash is None:
            raise ValueError('GENERATION_ADJUSTMENT_CASH_MISSING')
        adjustment = validate_generation_adjustment(record, decode_payload(cash['payload']), checkpoint=checkpoint)
        key = 'capital_flows' if adjustment['economic_type'] == 'capital_flow' else 'profit_adjustments'
        totals[key] += adjustment['amount']
    return totals
