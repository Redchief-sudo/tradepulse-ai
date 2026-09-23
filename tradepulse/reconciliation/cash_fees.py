"""Cash-fee expense allocation after authoritative closing-order attribution.

Order-level expense is allocated across its FIFO closures in proportion to
executed proceeds. This is an explicit accounting allocation, not a claim that
Alpaca supplied individual lot/fill fee amounts. The final allocation receives
the exact remainder. Intermediate allocations are rounded down to the receipt
amount precision, so no fractional rounding can create or lose an expense.
"""
from decimal import ROUND_DOWN, Decimal

from .asset_fees import AssetFeeIntegrityError


def allocate_cash_fees(proof, attributions):
    receipts = {row['id']: row for row in proof['activities']}
    plans = []
    for fee_id, order in proof.get('cash_fee_populations', {}).items():
        if order.get('method') not in {'authoritative_order_link', 'authoritative_fill_link'}:
            raise AssetFeeIntegrityError('CASH_FEE_AUTHORITATIVE_LINK_REQUIRED')
        selected = sorted((a for a in attributions if a.closing_trade_intent_id in order['trade_intent_ids']),
                          key=lambda a: a.attribution_id)
        if not selected:
            continue  # this fee belongs to another instrument's replay
        raw = receipts[fee_id]
        expense = -Decimal(raw['net_amount'])
        total = sum((a.quantity*a.exit_price for a in selected), Decimal(0))
        if expense <= 0 or total <= 0:
            raise AssetFeeIntegrityError('CASH_FEE_ALLOCATION_INVALID')
        quantum = Decimal(1).scaleb(expense.as_tuple().exponent)
        remaining = expense
        allocations = {}
        for i, attribution in enumerate(selected):
            amount = remaining if i == len(selected)-1 else (
                expense*(attribution.quantity*attribution.exit_price)/total
            ).quantize(quantum, rounding=ROUND_DOWN)
            if amount < 0 or amount > remaining:
                raise AssetFeeIntegrityError('CASH_FEE_ALLOCATION_INVALID')
            allocations[attribution.attribution_id] = amount
            remaining -= amount
        plans.append((raw, order, allocations))
    return plans
