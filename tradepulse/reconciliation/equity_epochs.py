"""Checkpoint non-crypto inventory and journal evidence from a complete feed.

Unlinked account fees are retained as unresolved expenses. They are never
assigned to an instrument by symbol coincidence, date-window guesses or rates.
"""
from decimal import Decimal
from datetime import UTC, datetime
from hashlib import sha256

from tradepulse.models import AssetClass, asset_identity_key
from tradepulse.persistence import hydrate
from tradepulse.persistence.codec import encode_payload
from tradepulse.settlement.accounting import project
from tradepulse.time import aware_utc

from .activity_cursor import activity_population, validate_pagination
from .epochs import finalize_population
from .fee_population import validate_fee_population


async def reconcile_equity_epochs(repositories, broker, *, now, clock=lambda: datetime.now(UTC)):
    now = aware_utc(now, field_name='equity_reconciliation_observed_at')
    # Skip a broker request only if there are no non-crypto fills to checkpoint.
    def assets(connection):
        from tradepulse.persistence.codec import decode_payload
        return {asset_identity_key(f.asset): f.asset for row in connection.execute('SELECT payload FROM fills')
                if (f := hydrate('fills', decode_payload(row['payload']))).asset.asset_class != AssetClass.CRYPTO}
    instruments = await repositories.fills.database.run(assets)
    if not instruments:
        return True
    try:
        raw, pagination = await activity_population(broker)
        positions = await broker.get_positions()
        now = aware_utc(clock(), field_name='equity_population_observed_at')
        from tradepulse.models import asset_key_from_broker_symbol
        quantities = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p.qty for p in positions}
        if len(quantities) != len(positions):
            raise ValueError('CHECKPOINT_DUPLICATE_POSITION_IDENTITY')
        validate_pagination(raw, pagination)

        def checkpoint(connection):
            from tradepulse.persistence.codec import decode_payload
            def rows(table):
                return [hydrate(table, decode_payload(row['payload'])) for row in connection.execute(f'SELECT payload FROM {table}')]
            fills, intents, lots, events = (rows(t) for t in ('fills', 'trade_intents', 'position_lots', 'settlements'))
            from .membership import classify_population, require_resolved
            from .generation_fees import persist_generation_fees
            membership = classify_population(connection, raw, pagination, now=now)
            require_resolved(membership)
            persist_generation_fees(connection, raw, membership, now=now)
            complete = True
            for key, asset in instruments.items():
                own_fills = [f for f in fills if asset_identity_key(f.asset) == key]
                own_events = [e for e in events if asset_identity_key(e.asset) == key]
                own_lots = [lot for lot in lots if asset_identity_key(lot.asset) == key]
                if {e.fill_id for e in own_events} != {f.fill_id for f in own_fills}:
                    raise ValueError('CHECKPOINT_SETTLEMENT_POPULATION_INCOMPLETE')
                for event in own_events:
                    if event.status.value != 'completed':
                        raise ValueError('CHECKPOINT_SETTLEMENT_INCOMPLETE')
                    project(connection, event, cash=True, trade=True)
                quantity = sum((lot.signed_quantity for lot in own_lots), Decimal(0))
                from .membership import opening_quantities
                baseline = opening_quantities(membership['checkpoint']).get(key, Decimal(0))
                if quantity + baseline != quantities.get(key, Decimal(0)):
                    raise ValueError('CHECKPOINT_POSITION_QUANTITY_MISMATCH:' + key)
                fees, proof = validate_fee_population(asset, raw, fills, intents, quantities.get(key, Decimal(0)), membership=membership)
                identifier = 'asset_fee_population:' + sha256(encode_payload(proof).encode()).hexdigest()
                record = {'record_id': identifier, 'reconciliation_type': 'accounting_population', 'subject_id': key,
                          'outcome': 'matched', 'expected': {'complete_instrument_population': True},
                          'actual': proof, 'occurred_at': now.isoformat()}
                connection.execute('INSERT OR IGNORE INTO reconciliation_records(record_id,payload,created_at) VALUES(?,?,?)',
                                   (identifier, encode_payload(record), now.isoformat()))
                finalize_population(connection, key=key, proof=proof, population_id=identifier, fills=own_fills,
                                    fees=fees, cash_plans=[], lots=own_lots, quantity=quantity, activities=raw,
                                    now=now, pagination=pagination, membership=membership)
                from .epochs import epochs_for
                complete = complete and all(e['fee_accounting_status'] == 'reconciled_net' for e in epochs_for(connection, key))
            return complete
        return await repositories.fills.database.run(checkpoint, write=True)
    except Exception as exc:
        # A failed new observation cannot leave an older checkpoint eligible.
        # Preserve its immutable receipt and supersede the mutable epoch state.
        from .epochs import block_asset
        for asset in instruments.values():
            await block_asset(repositories, asset, str(exc), now=now, pending=True)
        raise
