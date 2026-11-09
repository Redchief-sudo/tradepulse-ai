"""Operator closure of ambiguous dispatches and durable late-order detection.

Absence is an operator decision, never a consequence of a broker 404. This
module does not submit, cancel, or silently adopt any broker order.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tradepulse.execution import (
    SYMBOL_LOCK_TTL_SECONDS,
    execution_lock_key,
    release_symbol_reservation,
    reserve_symbol_for_execution,
)
from tradepulse.models import AuditEvent, IntegrityHold, IntegrityHoldType, TradeIntentStatus
from tradepulse.persistence import hydrate, run_with_lock_renewal
from tradepulse.persistence.codec import decode_payload, encode_payload
from tradepulse.risk import latch_financial_integrity_block

MINIMUM_DISPATCH_AGE = timedelta(minutes=15)
WATCH_DURATION = timedelta(days=30)
# Rev.154 R154-02: dispatched_at is the local clock; broker timestamps are the
# broker's. Every evidence window starts this much earlier, so a local clock
# running ahead can never push the delayed order, or its fill, out of view.
DISPATCH_EVIDENCE_SKEW = timedelta(minutes=5)
# An appeared order can be acknowledged only once it can no longer change.
# done_for_day can resume, and replaced hands its quantity to another order.
_ACKNOWLEDGEABLE_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


class DispatchResolutionRefused(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise DispatchResolutionRefused(reason)


def _symbol(value):
    return value.replace('/', '').upper()


async def resolve_dispatch(repositories, broker, trade_intent_id, reason, *, account_lease, clock=lambda: datetime.now(UTC)):
    """Caller must also hold the exclusive account runtime lease.

    The asset reservation is renewed across every read. The transaction fences
    its ownership and compares the entire intent before atomically creating the
    watch and rejecting the intent. Missing history is never empty history.
    """
    account_lease.verify()
    _require(isinstance(reason, str) and bool(reason.strip()), 'OPERATOR_REASON_REQUIRED')
    row = await repositories.trade_intents.get(trade_intent_id)
    _require(row is not None, 'INTENT_NOT_FOUND')
    intent = hydrate('trade_intents', row['payload'])
    database = repositories.trade_intents.database
    token = str(uuid4())
    _require(await reserve_symbol_for_execution(database, intent.asset, token), 'ASSET_RESERVATION_HELD')
    lost = asyncio.Event()

    async def on_lost():
        lost.set()

    async def work():
        from tradepulse.models import ReconciliationOutcome, ReconciliationRecord

        from .coordinator import _commit_stranded

        current = await repositories.trade_intents.get(trade_intent_id)
        _require(current is not None, 'INTENT_NOT_FOUND')
        original = current['payload']
        subject = hydrate('trade_intents', original)
        _require(subject.asset == intent.asset, 'INTENT_CHANGED')
        _require(subject.status in (TradeIntentStatus.SUBMITTED, TradeIntentStatus.SUBMISSION_UNKNOWN)
                 and not subject.broker_order_id, 'INTENT_NOT_AMBIGUOUS_DISPATCH')
        _require(subject.dispatched_at is not None, 'DISPATCH_TIMESTAMP_UNPROVEN')
        _require(clock() - subject.dispatched_at >= MINIMUM_DISPATCH_AGE, 'DISPATCH_TOO_RECENT')
        window_start = subject.dispatched_at - DISPATCH_EVIDENCE_SKEW
        account = subject.risk_snapshot.get('broker_account_number')
        account_lease.verify()
        observed = await broker.get_account()
        _require(account_lease.ref.account_number == observed.account_number
                 and account_lease.ref.venue == broker.venue, 'ACCOUNT_LEASE_MISMATCH')
        _require(bool(account) and observed.account_number == account, 'ACCOUNT_IDENTITY_UNPROVEN')
        _require(await broker.get_order_by_client_order_id(trade_intent_id) is None, 'BROKER_ORDER_PRESENT')
        pages: list[dict] = []
        orders = await broker.get_order_history(window_start, page_evidence=pages)
        _require(isinstance(orders, list) and pages and pages[-1].get('terminal') is True
                 and not any(p.get('terminal') for p in pages[:-1]), 'ORDER_HISTORY_INCOMPLETE')
        _require(all(isinstance(p.get('request'), dict) and p['request'].get('status') == 'all'
                     and isinstance(p.get('order_ids'), list) for p in pages), 'ORDER_HISTORY_PROOF_INVALID')
        ids = [o.broker_order_id for o in orders]
        _require(set(ids) == {i for p in pages for i in p['order_ids']}, 'ORDER_HISTORY_PROOF_MISMATCH')
        _require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids), 'ORDER_HISTORY_INVALID')
        for order in orders:
            _require(order.submitted_at is not None, 'ORDER_HISTORY_TIMESTAMP_MISSING')
            _require(isinstance(order.raw, dict) and isinstance(order.raw.get('client_order_id'), str)
                     and bool(order.raw['client_order_id'].strip()),
                     'ORDER_HISTORY_IDENTITY_MISSING')
            _require(order.raw['client_order_id'] != trade_intent_id, 'BROKER_ORDER_PRESENT')
            # A different id on the same asset may be the delayed dispatch
            # misidentified by the adapter. Only locally owned orders explain it.
            if _symbol(order.symbol) == _symbol(subject.asset.symbol):
                owned = await repositories.trade_intents.get(order.raw['client_order_id'])
                _require(owned is not None and owned['payload'].get('broker_order_id') == order.broker_order_id,
                         'UNEXPLAINED_ASSET_ORDER')
        # Full history audit includes independently paginated activities, not
        # just open orders (which omit an order already filled or cancelled).
        from .activity_cursor import activity_population
        activities, activity_proof = await activity_population(broker)
        unexplained = []
        for activity in activities:
            if activity.get('activity_type') != 'FILL':
                continue
            _require(isinstance(activity.get('symbol'), str), 'FILL_SYMBOL_MISSING')
            if _symbol(activity['symbol']) != _symbol(subject.asset.symbol):
                continue
            try:
                at = datetime.fromisoformat(activity['transaction_time'])
                _require(at.tzinfo is not None, 'FILL_TIMESTAMP_INVALID')
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise DispatchResolutionRefused('FILL_TIMESTAMP_INVALID') from exc
            if at < window_start:
                continue
            # Evidence must explain the exact broker execution in local books.
            found = await database.run(lambda c, a=activity: c.execute(
                'SELECT 1 FROM fills WHERE broker_fill_id=?', (a['id'],)).fetchone())
            if found is None or activity.get('order_id') not in ids:
                unexplained.append(activity['id'])
        _require(not unexplained, 'UNEXPLAINED_ASSET_FILL')
        # Repeat identity and exact lookup after the slower population reads.
        final_account = await broker.get_account()
        _require(final_account.account_number == account, 'ACCOUNT_IDENTITY_CHANGED')
        _require(await broker.get_order_by_client_order_id(trade_intent_id) is None, 'BROKER_ORDER_PRESENT')
        _require(not lost.is_set(), 'ASSET_RESERVATION_LOST')
        now = clock()
        evidence = {'account_number': account, 'venue': broker.venue, 'operator_reason': reason.strip(),
                    'dispatched_at': subject.dispatched_at.isoformat(), 'window_start': window_start.isoformat(),
                    'resolved_at': now.isoformat(),
                    'order_pages': pages, 'orders': [o.raw for o in orders],
                    'activities': activities, 'activity_proof': activity_proof,
                    'exact_lookup': 'not_found_before_and_after_history'}
        watch = {'trade_intent_id': trade_intent_id, 'account_number': account, 'venue': broker.venue,
                 'symbol': subject.asset.symbol, 'evidence': evidence}
        updated = replace(subject, status=TradeIntentStatus.REJECTED,
                          rejection_reason='SUBMISSION_ABSENT_OPERATOR_RESOLVED')
        record = ReconciliationRecord(str(uuid4()), 'order', trade_intent_id, ReconciliationOutcome.CORRECTED,
                                      expected={'dispatch_absent': True}, actual=evidence, occurred_at=now,
                                      corrective_action='Operator resolved absent dispatch; watching for 30 days')

        def commit(connection):
            account_lease.verify()
            result = _commit_stranded(connection, original, subject, updated, record, now,
                                       [(execution_lock_key(subject.asset), token)])
            _require(result == 'committed', result)
            connection.execute('INSERT INTO resolved_dispatch_watches(record_id,payload,created_at,expires_at) '
                               'VALUES(?,?,?,?)', (trade_intent_id, encode_payload(watch), now.isoformat(),
                                                  (now + WATCH_DURATION).isoformat()))
        await database.run(commit, write=True)
        return evidence

    try:
        return await run_with_lock_renewal(database, execution_lock_key(intent.asset), token,
                                           SYMBOL_LOCK_TTL_SECONDS, work(), on_renewal_failed=on_lost)
    finally:
        await release_symbol_reservation(database, intent.asset, token)


async def check_resolved_dispatches(repositories, broker, alerts, now):
    """Return number of appeared dispatches; retain appeared incidents indefinitely.

    The watch does not expire after detection. An integrity reset must fail its
    clean-reconciliation gate until the incident is investigated and repaired.
    """
    database = repositories.trade_intents.database
    # An acknowledged appearance (acknowledge_appearance) is closed: its order
    # was terminal and every fill accounted when the operator acknowledged it.
    rows = await database.run(lambda c: c.execute(
        'SELECT * FROM resolved_dispatch_watches WHERE acknowledged_at IS NULL '
        'AND (expires_at>=? OR appeared_order_id IS NOT NULL)',
        (now.isoformat(),)).fetchall())
    if not rows:
        return 0
    account = await broker.get_account()
    appeared = 0
    for row in rows:
        watch = decode_payload(row['payload'])
        _require(watch['account_number'] == account.account_number and watch['venue'] == broker.venue,
                 'DISPATCH_WATCH_ACCOUNT_MISMATCH')
        order = await broker.get_order_by_client_order_id(row['record_id'])
        identifier = row['appeared_order_id']
        if order is not None:
            _require(isinstance(order.broker_order_id, str) and bool(order.broker_order_id), 'WATCH_ORDER_ID_MISSING')
            identifier = order.broker_order_id
        if identifier is None:
            continue
        reason = f"RESOLVED_DISPATCH_APPEARED: {row['record_id']} broker order {identifier}"
        hold = IntegrityHold(identifier, row['record_id'], IntegrityHoldType.RESOLVED_DISPATCH_APPEARED, reason, now)
        event = AuditEvent('resolved_dispatch_appeared:' + row['record_id'], 'resolved_dispatch_appeared',
                           'critical', reason, now, entity_type='trade_intent', entity_id=row['record_id'],
                           details={'broker_order_id': identifier, 'resolution_watch_id': row['record_id']})

        def persist(connection, identifier=identifier, row=row, hold=hold, event=event):
            connection.execute('UPDATE resolved_dispatch_watches SET appeared_order_id=? WHERE record_id=?',
                               (identifier, row['record_id']))
            connection.execute('INSERT INTO integrity_holds(record_id,status,payload,created_at,updated_at) '
                               'VALUES(?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET status=excluded.status, '
                               'payload=excluded.payload,updated_at=excluded.updated_at',
                               (identifier, hold.hold_type.value, encode_payload(hold), now.isoformat(), now.isoformat()))
            connection.execute('INSERT OR IGNORE INTO audit_events(record_id,payload,created_at) VALUES(?,?,?)',
                               (event.event_id, encode_payload(event), now.isoformat()))
        await database.run(persist, write=True)
        await latch_financial_integrity_block(repositories, reason, clock=lambda: now)
        # Durable audit above survives a failed notification; alert_once retries delivery.
        from tradepulse.alerts import alert_once
        await alert_once(repositories.audit_events, alerts, event)
        appeared += 1
    return appeared


async def acknowledge_appearance(repositories, broker, trade_intent_id, reason, *, account_lease,
                                 clock=lambda: datetime.now(UTC)):
    """Close a RESOLVED_DISPATCH_APPEARED incident (Rev.154 R154-01).

    Accepted only when the appeared order can no longer change (a terminal
    broker status) and every one of its broker fills is accounted locally by
    exact ID: a Fill or a Rev.140 quarantine execution, the same test as the
    unresolved-execution cursor. The fills total the order's filled quantity.
    One transaction re-reads the watch, deletes the hold and records the
    acknowledgement. The session latch stays: reset-integrity then runs all of
    its own gates. Nothing here submits, cancels or adopts an order.
    """
    from tradepulse.models import ReconciliationOutcome, ReconciliationRecord

    from .activity_cursor import activity_population
    from .unresolved import _accounted

    account_lease.verify()
    _require(isinstance(reason, str) and bool(reason.strip()), 'OPERATOR_REASON_REQUIRED')
    database = repositories.trade_intents.database
    row = await database.run(lambda c: c.execute(
        'SELECT * FROM resolved_dispatch_watches WHERE record_id=?', (trade_intent_id,)).fetchone())
    _require(row is not None, 'DISPATCH_WATCH_NOT_FOUND')
    _require(row['appeared_order_id'] is not None, 'DISPATCH_NOT_APPEARED')
    _require(row['acknowledged_at'] is None, 'APPEARANCE_ALREADY_ACKNOWLEDGED')
    watch = decode_payload(row['payload'])
    appeared_id = row['appeared_order_id']
    observed = await broker.get_account()
    _require(watch['account_number'] == observed.account_number and watch['venue'] == broker.venue
             and account_lease.ref.account_number == observed.account_number and account_lease.ref.venue == broker.venue,
             'DISPATCH_WATCH_ACCOUNT_MISMATCH')
    order = await broker.get_order(appeared_id)
    _require(order.broker_order_id == appeared_id and order.raw.get('client_order_id') == trade_intent_id,
             'APPEARED_ORDER_IDENTITY_MISMATCH')
    _require(order.status in _ACKNOWLEDGEABLE_ORDER_STATUSES, 'APPEARED_ORDER_NOT_TERMINAL')
    activities, activity_proof = await activity_population(broker)
    fills = [a for a in activities if a.get('activity_type') == 'FILL' and a.get('order_id') == appeared_id]
    try:
        filled = sum((Decimal(str(a['qty'])) for a in fills), Decimal(0))
    except (KeyError, ArithmeticError, ValueError, TypeError) as exc:
        raise DispatchResolutionRefused('APPEARED_FILL_QUANTITY_INVALID') from exc
    _require(filled == order.filled_qty, 'APPEARED_FILLS_INCOMPLETE')
    fill_ids = sorted(str(a['id']) for a in fills)
    final = await broker.get_order(appeared_id)
    _require((final.status, final.filled_qty) == (order.status, order.filled_qty), 'APPEARED_ORDER_CHANGED')
    now = clock()
    acknowledgement = {'operator_reason': reason.strip(), 'acknowledged_at': now.isoformat(),
                       'broker_order_id': appeared_id, 'order_status': order.status,
                       'filled_qty': str(order.filled_qty), 'fill_activity_ids': fill_ids,
                       'order': dict(order.raw), 'activity_proof': activity_proof}
    record = ReconciliationRecord(str(uuid4()), 'order', trade_intent_id, ReconciliationOutcome.CORRECTED,
                                  expected={'resolved_dispatch_appearance_accounted': True}, actual=acknowledgement,
                                  occurred_at=now, corrective_action='Operator acknowledged a late-appearing dispatch')
    event = AuditEvent('resolved_dispatch_acknowledged:' + trade_intent_id, 'resolved_dispatch_acknowledged',
                       'warning', f"RESOLVED_DISPATCH_ACKNOWLEDGED: {trade_intent_id} broker order {appeared_id}",
                       now, entity_type='trade_intent', entity_id=trade_intent_id,
                       details={'broker_order_id': appeared_id, 'operator_reason': reason.strip()})

    def commit(connection):
        account_lease.verify()
        current = connection.execute('SELECT appeared_order_id, acknowledged_at FROM resolved_dispatch_watches '
                                     'WHERE record_id=?', (trade_intent_id,)).fetchone()
        _require(current is not None and current['appeared_order_id'] == appeared_id
                 and current['acknowledged_at'] is None, 'DISPATCH_WATCH_CHANGED')
        unaccounted = [fill_id for fill_id in fill_ids if not _accounted(connection, fill_id)]
        _require(not unaccounted, 'APPEARED_FILL_UNACCOUNTED: ' + ', '.join(unaccounted))
        connection.execute("DELETE FROM integrity_holds WHERE record_id=? AND status=?",
                           (appeared_id, IntegrityHoldType.RESOLVED_DISPATCH_APPEARED.value))
        connection.execute('UPDATE resolved_dispatch_watches SET acknowledged_at=?, acknowledgement=? WHERE record_id=?',
                           (now.isoformat(), encode_payload(acknowledgement), trade_intent_id))
        connection.execute('INSERT INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                           (record.record_id, encode_payload(record), now.isoformat()))
        connection.execute('INSERT OR IGNORE INTO audit_events(record_id,payload,created_at) VALUES(?,?,?)',
                           (event.event_id, encode_payload(event), now.isoformat()))
    await database.run(commit, write=True)
    return acknowledgement
