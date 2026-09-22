"""Pure historical fee replay preview. This module cannot write account state.

A preview is not fee attribution authority. Application requires independently
verified membership of every fee in the local accounting population.
"""
from dataclasses import replace
from decimal import Decimal

from tradepulse.models import Side, asset_identity_key
from tradepulse.settlement.lots import plan_signed_lot_fill

from .asset_fees import AssetFeeIntegrityError, parse_asset_fee


def preview_fee_replay(lots, settlements, fees):
    """Reconstruct long FIFO quantities and gross realized P&L from source events.

    Return replacement lots and per-settlement realized results. Preserve lot
    IDs, opened units, acquisition prices and observed price extrema. Source
    settlement quantities/prices are never altered. Equal fee timestamps use
    activity ID ordering across both fill and fee events.
    """
    if not lots:
        raise AssetFeeIntegrityError('ASSET_FEE_NO_LOCAL_LOTS')
    key = asset_identity_key(lots[0].asset)
    if any(asset_identity_key(x.asset) != key for x in [*lots, *settlements, *fees]):
        raise AssetFeeIntegrityError('ASSET_FEE_REPLAY_MIXED_ASSETS')
    if any(lot.position_side != 'long' for lot in lots):
        raise AssetFeeIntegrityError('ASSET_FEE_UNSUPPORTED_SHORT_INVENTORY')
    if len({fee.activity_id for fee in fees}) != len(fees):
        raise AssetFeeIntegrityError('ASSET_FEE_DUPLICATE_RECEIPT')
    if any(parse_asset_fee(fee.receipt) != fee for fee in fees):
        raise AssetFeeIntegrityError('ASSET_FEE_RECEIPT_FIELDS_MISMATCH')
    if {fid for lot in lots for fid in lot.asset_fee_quantities} - {fee.activity_id for fee in fees}:
        raise AssetFeeIntegrityError('ASSET_FEE_REPLAY_RECEIPT_MISSING')
    events = {e.fill_id: e for e in settlements}
    if len(events) != len(settlements):
        raise AssetFeeIntegrityError('ASSET_FEE_DUPLICATE_FILL')
    origins = {lot.originating_fill_id: lot for lot in lots}
    if len(origins) != len(lots):
        raise AssetFeeIntegrityError('ASSET_FEE_DUPLICATE_OPENING')
    for event in settlements:
        if event.status.value != 'completed' or not all((event.integrity_verified, event.lot_projected,
                event.holding_projected, event.attribution_projected, event.trade_projected, event.cash_projected)):
            raise AssetFeeIntegrityError('ASSET_FEE_REPLAY_UNSETTLED_EVENT')
    for lot in lots:
        event = events.get(lot.originating_fill_id)
        if (event is None or event.side != Side.BUY or event.quantity != lot.opened_quantity
                or event.price != lot.acquisition_price or event.occurred_at != lot.opened_at):
            raise AssetFeeIntegrityError('ASSET_FEE_OPENING_EVIDENCE_INVALID')
        if lot.remaining_quantity + sum(lot.closures.values(), Decimal(0)) + sum(lot.asset_fee_quantities.values(), Decimal(0)) != lot.opened_quantity:
            raise AssetFeeIntegrityError('ASSET_FEE_PRE_REPLAY_CONSERVATION_FAILED')
    if any(not e.broker_fill_id for e in settlements):
        raise AssetFeeIntegrityError('ASSET_FEE_BROKER_ACTIVITY_ID_MISSING')
    chronology = [(e.occurred_at, e.broker_fill_id, 'fill', e) for e in settlements]
    chronology += [(f.occurred_at, f.activity_id, 'fee', f) for f in fees]
    current = {}
    realized = {}
    for _, _, kind, event in sorted(chronology, key=lambda item: (item[0], item[1])):
        if kind == 'fee':
            remaining = event.quantity
            for lot in sorted(current.values(), key=lambda x: (x.opened_at, x.lot_id)):
                amount = min(remaining, lot.remaining_quantity)
                if amount <= 0:
                    continue
                current[lot.lot_id] = replace(lot, remaining_quantity=lot.remaining_quantity-amount,
                                            asset_fee_quantities={**lot.asset_fee_quantities, event.activity_id: amount})
                remaining -= amount
            if remaining:
                raise AssetFeeIntegrityError('ASSET_FEE_INSUFFICIENT_HISTORICAL_INVENTORY')
        elif event.side == Side.BUY:
            lot = origins.get(event.fill_id)
            if lot is None:
                raise AssetFeeIntegrityError('ASSET_FEE_OPENING_EVIDENCE_MISSING')
            current[lot.lot_id] = replace(lot, remaining_quantity=lot.opened_quantity, closures={},
                                        realized_pnl=Decimal(0), asset_fee_quantities={})
            realized[event.fill_id] = Decimal(0)
        else:
            plan = plan_signed_lot_fill(list(current.values()), event)
            if plan.opening_quantity:
                raise AssetFeeIntegrityError('ASSET_FEE_REPLAY_OVERSELL')
            for closure in plan.closures:
                lot = closure.lot
                current[lot.lot_id] = replace(lot, remaining_quantity=lot.remaining_quantity-closure.quantity,
                                            closures={**lot.closures, event.fill_id: closure.quantity},
                                            realized_pnl=lot.realized_pnl+closure.pnl)
            realized[event.fill_id] = plan.realized_pnl
    return [current[lot.lot_id] for lot in lots], realized
