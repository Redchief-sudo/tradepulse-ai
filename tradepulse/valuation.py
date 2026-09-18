"""Broker reporting valuation; pending risk reservations are not held assets."""
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from tradepulse.broker.types import AlpacaAccount, AlpacaPosition
from tradepulse.models import PortfolioSnapshot, ReconciliationOutcome, ReconciliationRecord
from tradepulse.models.market import asset_key_from_broker_symbol
from tradepulse.persistence import (
    PersistenceRepositories,
    hydrate,
    list_all_by_json_time_range,
    list_all_by_statuses,
    paginate_all_rows,
)
from tradepulse.risk.engine import _risk_day_bounds


async def marked_snapshot(
    repositories: PersistenceRepositories, account: AlpacaAccount, positions: Sequence[AlpacaPosition],
    *, now: datetime | None = None,
) -> PortfolioSnapshot:
    now = now or datetime.now(UTC)
    holdings = {r['record_id']: hydrate('holdings', r['payload']) for r in await paginate_all_rows(repositories.holdings)}
    market = Decimal(0)
    cost = Decimal(0)
    sectors = {}
    sector_cost = {}
    errors = []
    seen = set()
    for position in positions:
        key = asset_key_from_broker_symbol(position.asset_class, position.symbol)
        if key in seen:
            raise ValueError('duplicate broker position identity')
        seen.add(key)
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
        if signed_value != market:
            errors.append('broker_position_value_discrepancy')
    start, end = _risk_day_bounds(now)
    fills = await list_all_by_json_time_range(repositories.fills, 'filled_at', start, end)
    pending = await list_all_by_statuses(repositories.trade_intents, ['submitted', 'accepted', 'partially_filled', 'submission_unknown'])
    if account.last_equity <= 0:
        raise ValueError('broker previous close equity unavailable')
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
        equity_reconciliation_status='failed' if errors else 'matched', valuation_errors=tuple(errors),
    )


async def record_valuation(repositories: PersistenceRepositories, snapshot: PortfolioSnapshot) -> None:
    record = ReconciliationRecord(
        record_id=f'equity:{snapshot.snapshot_id}', reconciliation_type='equity', subject_id='broker_equity',
        outcome=ReconciliationOutcome.MATCHED if snapshot.equity_reconciliation_status == 'matched' else ReconciliationOutcome.DRIFT_DETECTED,
        expected={'total_equity': snapshot.total_equity},
        actual={'cash': snapshot.cash_balance, 'holdings_value': snapshot.holdings_value,
                'broker_components': snapshot.broker_equity_components,
                'difference': snapshot.equity_reconciliation_difference, 'errors': snapshot.valuation_errors},
        occurred_at=snapshot.as_of,
    )
    await repositories.reconciliation_records.create_once(record.record_id, record)
    if snapshot.equity_reconciliation_status == "failed":
        logging.getLogger(__name__).warning("equity_reconciliation_failed", extra={"snapshot_id": snapshot.snapshot_id, "valuation_errors": snapshot.valuation_errors})
