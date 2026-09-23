"""Exhaustive instrument-population proof, not guessed fee-to-fill linkage."""
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import asset_identity_key
from tradepulse.models.base import decimal_value, require_text
from tradepulse.time import aware_utc
from tradepulse.persistence.codec import encode_payload


def validate_fee_population(asset, activities, fills, intents, broker_quantity, *, membership=None):
    """Require the complete, unfiltered broker activity feed and exact local fills.

    The caller must obtain every page without a date/type filter. Every fill
    affecting this instrument must trace to a local intent's broker order, and
    every local fill must occur in that feed. Unknown inventory movements fail.
    The resulting net quantity must also match the fresh broker position. Other
    instruments do not confer ownership and cannot consume this inventory.
    """
    from .asset_fees import AssetFeeIntegrityError, parse_asset_fee
    from .generation_fees import cash_fee_receipt

    key = asset_identity_key(asset)
    symbol = asset.symbol.replace('/', '')
    local = {f.broker_fill_id: f for f in fills if asset_identity_key(f.asset) == key}
    intent_by_id = {i.trade_intent_id: i for i in intents}
    if not local or None in local or len(local) != sum(asset_identity_key(f.asset) == key for f in fills):
        raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_LOCAL_IDENTITY_INVALID')
    seen = set()
    matched = set()
    relevant = []
    cash_fees = []
    fees = []
    net = Decimal(0)
    from .membership import eligible_activities, activity_time
    eligible = eligible_activities(activities, membership) if membership else activities
    eligible_ids = {r['id'] for r in eligible}
    for raw in activities:
        identifier = require_text(raw.get('id'), 'activity_id')
        if identifier in seen:
            raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_DUPLICATE_ACTIVITY')
        seen.add(identifier)
        activity_time(raw)
        if identifier not in eligible_ids:
            continue
        kind = raw.get('activity_type')
        raw_symbol = str(raw.get('symbol') or '').replace('/', '')
        cash_fee = cash_fee_receipt(raw)
        if cash_fee is not None:
            cash_fees.append(dict(raw))
            continue
        # Cash-only non-trade activities have no inventory quantity. Do not
        # misclassify USD sell fees as asset-unit debits or invent their linkage.
        if not raw_symbol:
            cash_only = kind in {'JNLC', 'CSD', 'CSW', 'FEE', 'INT'} or (
                kind == 'CFEE' and raw.get('description') == 'Coin Pair Transaction Fee (USD)')
            if (not cash_only or raw.get('currency') != 'USD' or raw.get('status') != 'executed'
                    or decimal_value(raw.get('qty', '0'), 'cash_activity_qty') != 0):
                raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_UNCLASSIFIED_MOVEMENT')
            amount = decimal_value(raw['net_amount'], 'cash_activity_amount')
            continue
        if raw_symbol != symbol:
            continue
        relevant.append(dict(raw))
        if kind == 'FILL':
            if isinstance(raw.get('qty'), float) or isinstance(raw.get('price'), float):
                raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_INEXACT_RECEIPT')
            fill = local.get(identifier)
            intent = intent_by_id.get(fill.trade_intent_id) if fill else None
            at = aware_utc(raw.get('transaction_time'), field_name='activity_time')
            if (fill is None or intent is None or intent.broker_order_id != raw.get('order_id')
                    or fill.order_id != raw.get('order_id') or asset_identity_key(intent.asset) != key
                    or fill.side != intent.side or fill.execution_mode != intent.execution_mode
                    or fill.side.value != raw.get('side') or fill.filled_at != at
                    or fill.quantity != decimal_value(raw['qty'], 'activity_qty', positive=True)
                    or fill.price != decimal_value(raw['price'], 'activity_price', positive=True)):
                raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_EXTERNAL_OR_MISMATCHED_FILL')
            matched.add(identifier)
            net += fill.quantity if fill.side.value == 'buy' else -fill.quantity
        elif kind == 'CFEE':
            fee = parse_asset_fee(raw)
            fees.append(fee)
            net -= fee.quantity
        else:
            raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_UNCLASSIFIED_MOVEMENT')
    if matched != local.keys():
        raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_INCOMPLETE_FILL_HISTORY')
    from .membership import opening_quantities
    opening_quantity = opening_quantities(membership['checkpoint'] if membership else None).get(key, Decimal(0))
    if net + opening_quantity != broker_quantity:
        raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_BROKER_QUANTITY_MISMATCH')
    # Only a broker-provided order/fill relationship proves trade ownership.
    # Account-level fees remain in the generation ledger without guessed splits.
    cash_populations = {}
    all_local = {fill.broker_fill_id: fill for fill in fills}
    for cash in cash_fees:
        aware_utc(cash.get('created_at'), field_name='cash_fee_time')
        if not cash.get('order_id') and not cash.get('fill_id'):
            continue
        candidates = [raw for raw in eligible if raw.get('activity_type') == 'FILL'
                      and ((cash.get('order_id') and raw.get('order_id') == cash['order_id'])
                           or (cash.get('fill_id') and raw['id'] == cash['fill_id']))]
        if not candidates:
            raise AssetFeeIntegrityError('CASH_FEE_AUTHORITATIVE_LINK_MISSING')
        if any(all_local.get(raw['id']) is None for raw in candidates):
            raise AssetFeeIntegrityError('CASH_FEE_EXTERNAL_OR_MISMATCHED_SELL')
        selected = [all_local[raw['id']] for raw in candidates]
        if any(fill.side.value != 'sell' for fill in selected):
            continue  # authoritative entry fee stays in the generation cash ledger
        keys = {asset_identity_key(fill.asset) for fill in selected}
        if len(keys) != 1:
            raise AssetFeeIntegrityError('CASH_FEE_AUTHORITATIVE_LINK_AMBIGUOUS')
        if key not in keys:
            continue
        orders = {fill.order_id: fill.trade_intent_id for fill in selected}
        cash_populations[cash['id']] = {'broker_order_ids': sorted(orders),
            'trade_intent_ids': sorted(set(orders.values())), 'asset_key': key,
            'method': 'authoritative_order_link' if cash.get('order_id') else 'authoritative_fill_link'}
    relevant.sort(key=lambda row: row['id'])
    proof = {'method': 'complete_instrument_activity_population', 'asset_key': key,
             'activities': sorted((dict(row) for row in activities), key=lambda row: row['id']),
             'cash_fee_populations': cash_populations, 'broker_quantity': broker_quantity,
             'activity_digest': sha256(encode_payload(relevant).encode()).hexdigest()}
    if membership and membership.get('checkpoint'):
        proof['generation_membership'] = membership
        proof['opening_broker_quantity'] = opening_quantity
    return fees, proof
