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


def allocate_cash_fees_with_quarantine(proof, attributions, quarantined):
    """allocate_cash_fees, extended for an order whose execution created a
    quarantined record (Rev.140 §3.6). Such a fee is split signed by
    quarantine.fees.allocate_fee, with the quarantined records last, so the
    final one takes the exact remainder; ``quarantined`` maps a trade intent
    ID to its (record_id, proceeds). Every other fee is allocated exactly as
    before. Returns (plans, quarantine allocation shares)."""
    from tradepulse.quarantine.fees import allocate_fee

    linked = {fee_id for fee_id, order in proof.get('cash_fee_populations', {}).items()
              if any(quarantined.get(tid) for tid in order.get('trade_intent_ids', []))}
    plain = dict(proof)
    plain['cash_fee_populations'] = {k: v for k, v in proof.get('cash_fee_populations', {}).items() if k not in linked}
    plans = allocate_cash_fees(plain, attributions)
    receipts = {row['id']: row for row in proof['activities']}
    shares = []
    for fee_id in sorted(linked):
        order = proof['cash_fee_populations'][fee_id]
        if order.get('method') not in {'authoritative_order_link', 'authoritative_fill_link'}:
            raise AssetFeeIntegrityError('CASH_FEE_AUTHORITATIVE_LINK_REQUIRED')
        selected = sorted((a for a in attributions if a.closing_trade_intent_id in order['trade_intent_ids']),
                          key=lambda a: a.attribution_id)
        raw = receipts[fee_id]
        split = allocate_fee(fee_id, Decimal(raw['net_amount']),
                             [('attribution:' + a.attribution_id, a.quantity * a.exit_price) for a in selected],
                             [(rid, proceeds, 'opening') for tid in order['trade_intent_ids'] for rid, proceeds in quarantined.get(tid, [])])
        allocations = {share.record_id.removeprefix('attribution:'): -share.amount
                       for share in split if share.record_id.startswith('attribution:')}
        if allocations:
            plans.append((raw, order, allocations))
        shares.extend(share for share in split if not share.record_id.startswith('attribution:'))
    return plans, shares
