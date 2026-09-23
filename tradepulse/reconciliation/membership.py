"""Generation membership proven by immutable populations and cursor receipts.

Timestamps corroborate an established population boundary; they never create
one. The complete broker history remains in the inbox, including excluded rows.
"""
from hashlib import sha256

from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.time import aware_utc

from .activity_cursor import cursor_activity_id, validate_pagination

ELIGIBLE_MEMBERSHIPS = frozenset({'in_generation', 'late_arriving_in_generation'})


def activity_time(raw):
    field = 'transaction_time' if raw.get('activity_type') == 'FILL' else 'created_at'
    return aware_utc(raw.get(field), field_name='activity_' + field)


def _classify(raw, *, index, boundary_index, opening, opened, observed, orders, fills,
              closing=None, closing_index=-1):
    at = activity_time(raw)
    identifier = raw['id']
    if identifier in opening:
        return 'pre_generation', 'immutable_opening_population_member'
    if opened is None:
        return 'in_generation', 'unbound_operational_population'
    linked = raw.get('order_id') in orders or raw.get('fill_id') in fills or raw.get('broker_fill_id') in fills
    if at > observed:
        return 'unresolved_generation_membership', 'future_activity_timestamp'
    if at < opened:
        return 'unresolved_generation_membership', 'new_receipt_predates_opening'
    if closing and identifier not in {r['id'] for r in closing['activities']}:
        if linked:
            return 'late_arriving_in_generation', 'generation_link_after_sealed_population'
        if index > closing_index and at >= aware_utc(closing['sealed_at'], field_name='generation_sealed_at'):
            return 'post_generation', 'absent_at_seal_and_after_verified_closing_cursor'
        return 'unresolved_generation_membership', 'absent_at_seal_without_proven_closing_boundary'
    if index > boundary_index:
        return 'in_generation', 'absent_at_opening_and_after_verified_cursor'
    if linked:
        return 'late_arriving_in_generation', 'absent_at_opening_and_generation_order_or_fill_link'
    return 'unresolved_generation_membership', 'before_opening_cursor_without_generation_link'


def classify_population(connection, activities, pagination, *, now):
    """Persist exact source receipts and an immutable, reproducible membership proof.

    A backfilled row before the opening cursor needs an order/fill relationship
    to an intent admitted to this database. A date alone cannot establish that
    a newly observed but backdated activity belongs to the generation.
    """
    from tradepulse.verification.opening import load_bound_opening_checkpoint, load_bound_closing_checkpoint

    now = aware_utc(now, field_name='membership_observed_at')
    validate_pagination(activities, pagination)
    checkpoint = load_bound_opening_checkpoint(connection)
    closing = load_bound_closing_checkpoint(connection) if checkpoint else None
    classifications = {}
    reasons = {}
    opening = {r['id']: r for r in checkpoint['activities']} if checkpoint else {}
    current = {r['id']: r for r in activities}
    if checkpoint and any(current.get(identifier) != raw for identifier, raw in opening.items()):
        raise ValueError('GENERATION_OPENING_ACTIVITY_CHANGED_OR_MISSING')
    cursor = cursor_activity_id(checkpoint['opening_activity_cursor']) if checkpoint else None
    ids = [r['id'] for r in activities]
    boundary_index = ids.index(cursor) if cursor else -1
    closing_cursor = cursor_activity_id(closing['cursor']) if closing else None
    closing_index = ids.index(closing_cursor) if closing_cursor else -1
    if closing and any(current.get(raw['id']) != raw for raw in closing['activities']):
        raise ValueError('GENERATION_SEALED_ACTIVITY_CHANGED_OR_MISSING')
    generation_orders = set()
    generation_fills = set()
    if checkpoint:
        for row in connection.execute('SELECT payload FROM trade_intents'):
            intent = decode_payload(row['payload'])
            # A clean, bound database cannot contain pre-generation intents.
            if intent.get('broker_order_id'):
                generation_orders.add(intent['broker_order_id'])
        for row in connection.execute('SELECT payload FROM fills'):
            fill = decode_payload(row['payload'])
            if fill.get('broker_fill_id'):
                generation_fills.add(fill['broker_fill_id'])
    for index, raw in enumerate(activities):
        identifier = raw['id']
        status, reason = _classify(raw, index=index, boundary_index=boundary_index, opening=opening,
            opened=aware_utc(checkpoint['opened_at'], field_name='generation_opened_at') if checkpoint else None,
            observed=now, orders=generation_orders, fills=generation_fills,
            closing=closing, closing_index=closing_index)
        classifications[identifier] = status
        reasons[identifier] = reason
        existing = connection.execute('SELECT payload FROM broker_activity_inbox WHERE record_id=?', (identifier,)).fetchone()
        if existing and decode_payload(existing['payload']) != raw:
            raise ValueError('BROKER_ACTIVITY_RECEIPT_CHANGED:' + identifier)
        connection.execute('INSERT OR IGNORE INTO broker_activity_inbox(record_id,payload,created_at) VALUES(?,?,?)',
                           (identifier, encode_payload(raw), now.isoformat()))
    result = {'classifications': classifications, 'reasons': reasons,
              'checkpoint': checkpoint, 'population_hash': pagination['population_hash']}
    if checkpoint:
        body = {'verification_generation_id': checkpoint['verification_generation_id'],
                'generation_opening_checkpoint_id': checkpoint['checkpoint_id'],
                'opening_activity_cursor': checkpoint['opening_activity_cursor'],
                'opening_activity_population_hash': checkpoint['opening_activity_population_hash'],
                'population_hash': pagination['population_hash'], 'activities': activities,
                'generation_order_ids': sorted(generation_orders), 'generation_fill_ids': sorted(generation_fills),
                'classifications': classifications,
                'reasons': reasons, 'pagination': pagination, 'closing_checkpoint': closing}
        identifier = 'generation_membership:' + sha256(encode_payload(body).encode()).hexdigest()
        unresolved = any(s == 'unresolved_generation_membership' for s in classifications.values())
        record = {'record_id': identifier, 'reconciliation_type': 'generation_membership',
                  'subject_id': checkpoint['verification_generation_id'],
                  'outcome': 'drift_detected' if unresolved else 'matched',
                  'expected': {'all_activity_membership_resolved': True}, 'actual': body,
                  'occurred_at': now.isoformat(), 'corrective_action': 'immutable_generation_membership_proof'}
        connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (identifier, encode_payload(record), now.isoformat()))
        result['record_id'] = identifier
    return result


def require_resolved(membership):
    unresolved = sorted(i for i, status in membership['classifications'].items()
                        if status == 'unresolved_generation_membership')
    if unresolved:
        raise ValueError('UNRESOLVED_GENERATION_MEMBERSHIP:' + ','.join(unresolved))


def eligible_activities(activities, membership):
    require_resolved(membership)
    return [r for r in activities if membership['classifications'][r['id']] in ELIGIBLE_MEMBERSHIPS]


def verify_membership_record(checkpoint, record):
    """Independently check the immutable population boundary for reports."""
    body = record['actual']
    if (record['record_id'] != 'generation_membership:' + sha256(encode_payload(body).encode()).hexdigest()
            or body['verification_generation_id'] != checkpoint['verification_generation_id']
            or body['generation_opening_checkpoint_id'] != checkpoint['checkpoint_id']
            or body['opening_activity_cursor'] != checkpoint['opening_activity_cursor']
            or body['opening_activity_population_hash'] != checkpoint['opening_activity_population_hash']):
        raise ValueError('GENERATION_MEMBERSHIP_PROOF_MISMATCH')
    activities = body['activities']
    validate_pagination(activities, body['pagination'])
    if body['population_hash'] != body['pagination']['population_hash']:
        raise ValueError('GENERATION_MEMBERSHIP_POPULATION_MISMATCH')
    opening = {r['id']: r for r in checkpoint['activities']}
    current = {r['id']: r for r in activities}
    if any(current.get(i) != raw for i, raw in opening.items()):
        raise ValueError('GENERATION_OPENING_ACTIVITY_CHANGED_OR_MISSING')
    ids = [r['id'] for r in activities]
    cursor = cursor_activity_id(checkpoint['opening_activity_cursor'])
    cursor_index = ids.index(cursor) if cursor else -1
    if set(body['classifications']) != set(ids):
        raise ValueError('GENERATION_MEMBERSHIP_POPULATION_INCOMPLETE')
    observed = aware_utc(record['occurred_at'], field_name='membership_observed_at')
    opened = aware_utc(checkpoint['opened_at'], field_name='generation_opened_at')
    closing = body.get('closing_checkpoint')
    closing_cursor = cursor_activity_id(closing['cursor']) if closing else None
    closing_index = ids.index(closing_cursor) if closing_cursor else -1
    if closing:
        if (sha256(encode_payload(closing['activities']).encode()).hexdigest() != closing['population_hash']
                or any(current.get(raw['id']) != raw for raw in closing['activities'])):
            raise ValueError('GENERATION_SEALED_POPULATION_MISMATCH')
        aware_utc(closing['sealed_at'], field_name='generation_sealed_at')
    for index, raw in enumerate(activities):
        expected, reason = _classify(raw, index=index, boundary_index=cursor_index, opening=opening,
            opened=opened, observed=observed, orders=body['generation_order_ids'], fills=body['generation_fill_ids'],
            closing=closing, closing_index=closing_index)
        if body['classifications'][raw['id']] != expected or body['reasons'][raw['id']] != reason:
            raise ValueError('GENERATION_MEMBERSHIP_CLASSIFICATION_INVALID')
    unresolved = 'unresolved_generation_membership' in body['classifications'].values()
    if record['outcome'] != ('drift_detected' if unresolved else 'matched'):
        raise ValueError('GENERATION_MEMBERSHIP_OUTCOME_INVALID')
    return body['classifications']


def opening_quantities(checkpoint):
    """Broker inventory already owned at opening remains outside generation lots."""
    from decimal import Decimal
    from tradepulse.models import AssetClass, asset_key_from_broker_symbol
    if checkpoint is None:
        return {}
    return {asset_key_from_broker_symbol(AssetClass(p['asset_class']), p['symbol']): Decimal(p['qty'])
            for p in checkpoint['positions']}
