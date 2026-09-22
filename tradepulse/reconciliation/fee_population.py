"""Exhaustive instrument-population proof, not guessed fee-to-fill linkage."""
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from tradepulse.models import asset_identity_key
from tradepulse.models.base import decimal_value, require_aware, require_text
from tradepulse.persistence.codec import encode_payload


def validate_fee_population(asset, activities, fills, intents, broker_quantity):
    """Require the complete, unfiltered broker activity feed and exact local fills.

    The caller must obtain every page without a date/type filter. Every fill
    affecting this instrument must trace to a local intent's broker order, and
    every local fill must occur in that feed. Unknown inventory movements fail.
    The resulting net quantity must also match the fresh broker position. Other
    instruments do not confer ownership and cannot consume this inventory.
    """
    from .asset_fees import AssetFeeIntegrityError, parse_asset_fee

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
    for raw in activities:
        identifier = require_text(raw.get('id'), 'activity_id')
        if identifier in seen:
            raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_DUPLICATE_ACTIVITY')
        seen.add(identifier)
        kind = raw.get('activity_type')
        raw_symbol = str(raw.get('symbol') or '').replace('/', '')
        # Cash-only non-trade activities have no inventory quantity. Do not
        # misclassify USD sell fees as asset-unit debits or invent their linkage.
        if not raw_symbol:
            cash_only = kind in {'JNLC', 'CSD', 'CSW', 'FEE', 'INT'} or (
                kind == 'CFEE' and raw.get('description') == 'Coin Pair Transaction Fee (USD)')
            if (not cash_only or raw.get('currency') != 'USD' or raw.get('status') != 'executed'
                    or decimal_value(raw.get('qty', '0'), 'cash_activity_qty') != 0):
                raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_UNCLASSIFIED_MOVEMENT')
            amount = decimal_value(raw['net_amount'], 'cash_activity_amount')
            if kind == 'CFEE':
                if isinstance(raw['net_amount'], float) or amount >= 0:
                    raise AssetFeeIntegrityError('CASH_FEE_UNSUPPORTED_RECEIPT')
                cash_fees.append(dict(raw))
            continue
        if raw_symbol != symbol:
            continue
        relevant.append(dict(raw))
        if kind == 'FILL':
            if isinstance(raw.get('qty'), float) or isinstance(raw.get('price'), float):
                raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_INEXACT_RECEIPT')
            fill = local.get(identifier)
            intent = intent_by_id.get(fill.trade_intent_id) if fill else None
            at = require_aware(datetime.fromisoformat(raw['transaction_time']), 'activity_time')
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
    if net != broker_quantity:
        raise AssetFeeIntegrityError('ASSET_FEE_POPULATION_BROKER_QUANTITY_MISMATCH')
    # An unlinked cash receipt proves an expense for an exhaustive sell
    # population, not an individual order. When that population contains only
    # this canonical instrument, allocate the expense across the population's
    # proceeds. Never guess ownership across different instruments.
    from tradepulse.broker.symbols import infer_alpaca_asset_class, normalize_alpaca_symbol
    from tradepulse.models import AssetClass
    cash_populations = {}
    all_local = {fill.broker_fill_id: fill for fill in fills}
    for cash in cash_fees:
        at = require_aware(datetime.fromisoformat(cash['created_at']), 'cash_fee_time')
        candidates = []
        for raw in activities:
            raw_symbol = str(raw.get('symbol') or '')
            if (raw.get('activity_type') != 'FILL' or raw.get('side') != 'sell'
                    or infer_alpaca_asset_class(raw_symbol) != AssetClass.CRYPTO
                    or not raw_symbol.replace('/', '').endswith('USD')):
                continue
            filled_at = require_aware(datetime.fromisoformat(raw['transaction_time']), 'cash_fee_fill_time')
            if filled_at <= at and (not cash.get('order_id') or cash['order_id'] == raw.get('order_id')):
                candidates.append(raw)
        symbols = {normalize_alpaca_symbol(raw['symbol'], AssetClass.CRYPTO) for raw in candidates}
        if asset.symbol not in symbols:
            continue  # this instrument cannot consume another instrument's fee
        if len(symbols) != 1:
            raise AssetFeeIntegrityError('CASH_FEE_ASSET_POPULATION_AMBIGUOUS')
        orders = {}
        for raw in candidates:
            fill = all_local.get(raw['id'])
            intent = intent_by_id.get(fill.trade_intent_id) if fill else None
            filled_at = require_aware(datetime.fromisoformat(raw['transaction_time']), 'cash_fee_fill_time')
            if (fill is None or intent is None or fill.order_id != raw.get('order_id')
                    or intent.broker_order_id != fill.order_id or fill.side.value != 'sell'
                    or fill.side != intent.side or fill.execution_mode != intent.execution_mode
                    or asset_identity_key(fill.asset) != key or asset_identity_key(intent.asset) != key
                    or fill.filled_at != filled_at or fill.quantity != decimal_value(raw['qty'], 'sell_qty')
                    or fill.price != decimal_value(raw['price'], 'sell_price')):
                raise AssetFeeIntegrityError('CASH_FEE_EXTERNAL_OR_MISMATCHED_SELL')
            orders[fill.order_id] = fill.trade_intent_id
        cash_populations[cash['id']] = {'broker_order_ids': sorted(orders),
            'trade_intent_ids': sorted(set(orders.values())), 'asset_key': key,
            'method': 'authoritative_order_link' if cash.get('order_id') else 'exhaustive_single_asset_sell_population'}
    relevant.sort(key=lambda row: row['id'])
    proof = {'method': 'complete_instrument_activity_population', 'asset_key': key,
             'activities': sorted((dict(row) for row in activities), key=lambda row: row['id']),
             'cash_fee_populations': cash_populations, 'broker_quantity': broker_quantity,
             'activity_digest': sha256(encode_payload(relevant).encode()).hexdigest()}
    return fees, proof
