"""Broker reporting valuation; pending risk reservations are not held assets."""
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from tradepulse.broker.types import AlpacaAccount, AlpacaPosition
from tradepulse.models import PortfolioSnapshot, ReconciliationOutcome, ReconciliationRecord, asset_identity_key
from tradepulse.models.market import asset_key_from_broker_symbol
from tradepulse.persistence import (
    PersistenceRepositories,
    hydrate,
    list_all_by_json_time_range,
    list_all_by_statuses,
    paginate_all_rows,
)
from tradepulse.risk.engine import _risk_day_bounds
from tradepulse.time import aware_utc
from tradepulse.persistence.codec import decode_payload, encode_payload


def broker_settled_cash(entry) -> Decimal:
    """Cash the broker actually posts for one canonical ledger entry.

    Alpaca settles each individual fill's cash to the nearest cent, so an exact
    fractional-share notional (e.g. 0.124 x 335.94 = 41.65656) posts as 41.66.
    The immutable ledger keeps the exact amount; only the comparison against
    broker cash applies the per-fill cent settlement. Non-fill entries are
    broker-reported cent amounts already and pass through unchanged.
    """
    amount = Decimal(entry['amount'])
    if entry['entry_id'].startswith('fill:cash:'):
        return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return amount


def _generation_invariants(connection, account, positions):
    """Reconcile an official population against its immutable opening balances."""
    from tradepulse.verification.opening import load_bound_opening_checkpoint
    from tradepulse.reconciliation.membership import verify_membership_record
    from tradepulse.reconciliation.generation_fees import (
        adjustment_receipt, cash_fee_receipt, generation_adjustment_totals,
        validate_generation_adjustment, validate_generation_fee,
    )

    opening = load_bound_opening_checkpoint(connection)
    if opening is None:
        return None
    aware_utc(account.received_at, field_name='account_response_received_at')
    for position in positions:
        aware_utc(position.received_at, field_name='position_response_received_at')
    records = [decode_payload(r['payload']) for r in connection.execute('SELECT payload FROM reconciliation_records ORDER BY rowid')]
    populations = [r for r in records if r['reconciliation_type'] == 'generation_membership']
    if not populations:
        raise ValueError('generation_activity_population_missing')
    latest = populations[-1]
    classifications = verify_membership_record(opening, latest)
    if any(s == 'unresolved_generation_membership' for s in classifications.values()):
        raise ValueError('unresolved_generation_membership')
    by_record = {r['record_id']: r for r in records}
    entries = {r['record_id']: decode_payload(r['payload']) for r in connection.execute('SELECT record_id,payload FROM cash_ledger')}
    eligible = [a for a in latest['actual']['activities'] if classifications[a['id']] in {'in_generation', 'late_arriving_in_generation'}]
    for raw in eligible:
        if cash_fee_receipt(raw) is not None:
            validate_generation_fee(by_record['generation_fee:' + raw['id']], entries['broker:fee:' + raw['id']],
                                    raw=raw, checkpoint=opening)
        if adjustment_receipt(raw) is not None:
            validate_generation_adjustment(by_record['generation_adjustment:' + raw['id']],
                                           entries['broker:adjustment:' + raw['id']], raw=raw, checkpoint=opening)
    cash_movement = sum((broker_settled_cash(r) for r in entries.values()), Decimal(0))
    if any(r['currency'] != 'USD' for r in entries.values()):
        raise ValueError('generation_cash_currency_unverified')
    expected_cash = Decimal(opening['cash']) + cash_movement
    cash_status = 'matched' if expected_cash == account.cash else 'mismatch'
    opening_quantities = {}
    prior_inventory_change = Decimal(0)
    current = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p for p in positions}
    from tradepulse.models import AssetClass
    for old in opening['positions']:
        key = asset_key_from_broker_symbol(AssetClass(old['asset_class']), old['symbol'])
        quantity = Decimal(old['qty'])
        opening_quantities[key] = quantity
        if quantity:
            position = current.get(key)
            if position is None or not position.qty:
                raise ValueError('opening_inventory_mark_missing:' + key)
            prior_inventory_change += quantity * position.market_value / position.qty - Decimal(old['market_value'])
    capital = generation_adjustment_totals(connection)['capital_flows']
    return {
        'checkpoint_id': opening['checkpoint_id'], 'opening_quantities': opening_quantities,
        'population_record_id': latest['record_id'], 'population_status': 'matched',
        'generation_cash': {'status': cash_status, 'expected_cash': str(expected_cash),
                            'observed_cash': str(account.cash), 'difference': str(account.cash - expected_cash)},
        'generation_equity': {'status': cash_status,
                              'total_equity': str(account.equity - prior_inventory_change - capital),
                              'excluded_opening_inventory_change': str(prior_inventory_change),
                              'excluded_capital_flows': str(capital)},
    }


async def marked_snapshot(
    repositories: PersistenceRepositories, account: AlpacaAccount, positions: Sequence[AlpacaPosition],
    *, now: datetime | None = None,
) -> PortfolioSnapshot:
    now = aware_utc(now or datetime.now(UTC), field_name='valuation_observed_at')
    holdings = {r['record_id']: hydrate('holdings', r['payload']) for r in await paginate_all_rows(repositories.holdings)}
    market = Decimal(0)
    cost = Decimal(0)
    sectors = {}
    sector_cost = {}
    errors = []
    generation = None
    generation_failed = False
    try:
        generation = await repositories.fills.database.run(lambda c: _generation_invariants(c, account, positions))
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        generation_failed = True
        errors.append('generation_evidence_invalid:' + str(exc))
    seen = set()
    observation_times = {}
    if account.received_at is not None:
        observation_times['account_response_received_at'] = account.received_at.isoformat()
    for position in positions:
        key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
        if key in seen:
            raise ValueError('duplicate broker position identity')
        seen.add(key)
        if position.received_at is not None:
            observation_times['position_response_received_at:' + key] = position.received_at.isoformat()
        if not position.market_value.is_finite():
            raise ValueError('invalid broker market value')
        holding = holdings.get(key)
        sector = (holding.sector if holding else None) or 'Other'
        market += position.market_value
        sectors[sector] = sectors.get(sector, Decimal(0)) + position.market_value
        if position.cost_basis is None:
            errors.append(f'cost_basis_missing:{key}')
        elif not position.cost_basis.is_finite():
            raise ValueError('invalid broker cost basis')
        else:
            cost += position.cost_basis
            sector_cost[sector] = sector_cost.get(sector, Decimal(0)) + position.cost_basis
    components = dict(account.equity_components)
    difference = None
    observation_difference = None
    if not {'long_market_value', 'short_market_value'} <= components.keys():
        errors.append('broker_equity_components_missing')
    else:
        signed_value = components['long_market_value'] + components['short_market_value']
        # Cash already includes posted cash movements. Preserve all supplied
        # components, but never subtract accrued fees or transfers a second time.
        explained = account.cash + signed_value
        if 'memoposts' in components:
            explained -= components['memoposts']
        difference = account.equity - explained
        if difference != 0:
            errors.append('broker_equity_discrepancy')
        # Separate HTTP responses have no common broker valuation timestamp.
        # Preserve their exact difference; it cannot prove an equity arithmetic
        # failure, quantity drift, or a harmless quote move. No tolerance hides it.
        observation_difference = market - signed_value
    start, end = _risk_day_bounds(now)
    fills = await list_all_by_json_time_range(repositories.fills, 'filled_at', start, end)
    pending = await list_all_by_statuses(repositories.trade_intents, ['submitted', 'accepted', 'partially_filled', 'submission_unknown'])
    if account.last_equity <= 0:
        raise ValueError('broker previous close equity unavailable')
    epoch_rows = await paginate_all_rows(repositories.accounting_epochs)
    states = {}
    for row in epoch_rows:
        key, state = row['payload']['canonical_asset_key'], row['payload']['fee_accounting_status']
        if key not in states or state != 'reconciled_net':
            states[key] = state
    lot_quantities = {}
    for row in await paginate_all_rows(repositories.position_lots):
        lot = hydrate('position_lots', row['payload'])
        key = asset_identity_key(lot.asset)
        lot_quantities[key] = lot_quantities.get(key, Decimal(0)) + lot.signed_quantity
    broker_quantities = {asset_key_from_broker_symbol(p.asset_class, p.symbol): p.qty for p in positions}
    if generation is not None:
        for key, quantity in generation['opening_quantities'].items():
            broker_quantities[key] = broker_quantities.get(key, Decimal(0)) - quantity
    identities = set(holdings) | set(lot_quantities) | set(broker_quantities)
    quantities = {key: 'matched' if broker_quantities.get(key, Decimal(0)) ==
                  lot_quantities.get(key, Decimal(0)) else 'mismatch' for key in identities}
    lots_result = {key: 'matched' if lot_quantities.get(key, Decimal(0)) ==
                   (holdings[key].quantity if key in holdings else Decimal(0)) else 'mismatch' for key in identities}
    from tradepulse.settlement.accounting import accounting_issues
    projection_issues = await accounting_issues(repositories)
    holds = await paginate_all_rows(repositories.integrity_holds)
    results = {'projection_evidence': projection_issues, 'integrity_holds': len(holds),
               'broker_equity': 'matched' if difference == 0 else 'failed',
               'position_quantities': quantities, 'holdings_versus_lots': lots_result,
               'fee_finality': states,
               'prove_edge': {key: 'ineligible' if quantities[key] != 'matched' or lots_result[key] != 'matched'
                             or states.get(key) != 'reconciled_net' or bool(projection_issues) or bool(holds)
                             else 'requires_trade_evidence_verification' for key in identities}}
    results['broker_observation'] = {
        'account': decode_payload(encode_payload(account)),
        'positions': [decode_payload(encode_payload(position)) for position in positions],
    }
    if generation is not None:
        results.update({key: value for key, value in generation.items() if key != 'opening_quantities'})
    if generation_failed:
        results['generation_checkpoint'] = 'invalid'
    mandatory_match = (difference == 0 and not errors and not projection_issues and not holds
                       and all(v == 'matched' for v in (*quantities.values(), *lots_result.values()))
                       and all(v == 'reconciled_net' for v in states.values())
                       and all(states.get(key) == 'reconciled_net' for key in set(lot_quantities) | set(holdings))
                       and observation_difference == 0
                       and not generation_failed
                       and (generation is None or generation['generation_cash']['status'] == 'matched'))
    results['mandatory_invariants'] = 'matched' if mandatory_match else 'incomplete_or_mismatched'
    if generation is not None and not mandatory_match:
        results['generation_equity']['status'] = 'incomplete_or_mismatched'
    gross_by_asset = {}
    for row in await paginate_all_rows(repositories.trade_attributions):
        attr = hydrate('trade_attributions', row['payload'])
        key = asset_identity_key(attr.asset)
        gross_by_asset[key] = gross_by_asset.get(key, Decimal(0)) + attr.realized_pnl
    known_by_asset = {}
    for row in await paginate_all_rows(repositories.pnl_records):
        pnl = hydrate('pnl_records', row['payload'])
        key = asset_identity_key(pnl.asset)
        known_by_asset[key] = known_by_asset.get(key, Decimal(0)) + pnl.realized
    results['realized_pnl'] = {key: {
        'gross': str(gross_by_asset.get(key, Decimal(0))),
        'recognized_expenses': str(gross_by_asset.get(key, Decimal(0)) - known_by_asset.get(key, Decimal(0))),
        'net': str(known_by_asset.get(key, Decimal(0))) if states.get(key) == 'reconciled_net' and not projection_issues else None,
        'net_status': 'verified_checkpoint' if states.get(key) == 'reconciled_net' and not projection_issues else 'unresolved_fee_or_projection_evidence',
    } for key in identities}
    return PortfolioSnapshot(
        snapshot_id=str(uuid4()), as_of=now, total_equity=account.equity,
        cash_balance=account.cash, holdings_value=market, sector_exposure=sectors,
        open_positions=len(positions), outstanding_orders=len(pending),
        trades_today=len({r['payload']['trade_intent_id'] for r in fills}),
        daily_pnl_pct=(account.equity-account.last_equity)/account.last_equity*100,
        source='broker', valuation_version=2,
        holdings_cost_basis=None if any(e.startswith('cost_basis_missing:') for e in errors) else cost,
        sector_cost_basis={} if any(e.startswith('cost_basis_missing:') for e in errors) else sector_cost,
        broker_equity_components=components, equity_reconciliation_difference=difference,
        equity_reconciliation_status='matched' if mandatory_match else 'failed', valuation_errors=tuple(errors),
        position_value_observation_difference=observation_difference,
        valuation_observation_times=observation_times, accounting_states=states, reconciliation_results=results,
        position_value_observation_status=(
            'unavailable' if observation_difference is None else
            'equal_uncoordinated_observations' if observation_difference == 0 else
            'different_uncoordinated_observations'
        ),
    )


def reconciliation_outcome(snapshot: PortfolioSnapshot) -> ReconciliationOutcome:
    results = snapshot.reconciliation_results
    if snapshot.equity_reconciliation_difference not in (None, Decimal(0)):
        return ReconciliationOutcome.UNRESOLVED_MISMATCH
    if any(value != 'matched' for field in ('position_quantities', 'holdings_versus_lots')
           for value in results.get(field, {}).values()) or results.get('integrity_holds'):
        return ReconciliationOutcome.UNRESOLVED_MISMATCH
    if (snapshot.equity_reconciliation_difference is None or results.get('projection_evidence')
            or results.get('mandatory_invariants') != 'matched'):
        return ReconciliationOutcome.INCOMPLETE_EVIDENCE
    if snapshot.position_value_observation_difference not in (None, Decimal(0)):
        # Uncoordinated responses cannot prove a transient market move.
        return ReconciliationOutcome.INCOMPLETE_EVIDENCE
    return ReconciliationOutcome.MATCHED


def valuation_record(snapshot: PortfolioSnapshot) -> ReconciliationRecord:
    """Build the same valuation receipt for standalone and atomic reset writes."""
    return ReconciliationRecord(
        record_id=f'equity:{snapshot.snapshot_id}', reconciliation_type='equity', subject_id='broker_equity',
        outcome=reconciliation_outcome(snapshot),
        expected={'total_equity': snapshot.total_equity},
        actual={'cash': snapshot.cash_balance, 'holdings_value': snapshot.holdings_value,
                'broker_components': snapshot.broker_equity_components,
                'difference': snapshot.equity_reconciliation_difference, 'errors': snapshot.valuation_errors,
                'position_value_observation_difference': snapshot.position_value_observation_difference,
                'position_value_observation_status': snapshot.position_value_observation_status,
                'response_received_times': snapshot.valuation_observation_times,
                'independent_results': snapshot.reconciliation_results, 'accounting_states': snapshot.accounting_states},
        occurred_at=snapshot.as_of,
    )


async def record_valuation(repositories: PersistenceRepositories, snapshot: PortfolioSnapshot) -> None:
    record = valuation_record(snapshot)
    outcome = record.outcome
    await repositories.reconciliation_records.create_once(record.record_id, record)
    if outcome == ReconciliationOutcome.UNRESOLVED_MISMATCH:
        from tradepulse.risk import latch_financial_integrity_block
        await latch_financial_integrity_block(repositories, 'Mandatory accounting invariant mismatch', clock=lambda: snapshot.as_of)
    if snapshot.equity_reconciliation_status == "failed":
        logging.getLogger(__name__).warning("equity_reconciliation_failed", extra={"snapshot_id": snapshot.snapshot_id, "valuation_errors": snapshot.valuation_errors})
