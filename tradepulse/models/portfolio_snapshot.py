from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping

from .base import decimal_value, immutable_metadata, require_aware, require_text

SnapshotSource = Literal["broker", "holdings"]
_SOURCES = {"broker", "holdings"}


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """One point on the equity curve, persisted every scan/reconcile cycle so
    check_max_drawdown() has a dedicated series instead of overloading
    PnlRecord (which only tracks realized/unrealized PnL per asset, not
    account-level equity).
    """

    snapshot_id: str
    as_of: datetime
    total_equity: Decimal
    cash_balance: Decimal
    holdings_value: Decimal
    sector_exposure: Mapping[str, Decimal]
    open_positions: int
    outstanding_orders: int
    trades_today: int
    daily_pnl_pct: Decimal
    source: SnapshotSource
    valuation_version: int = 1
    holdings_cost_basis: Decimal | None = None
    sector_cost_basis: Mapping[str, Decimal] = field(default_factory=dict)
    broker_equity_components: Mapping[str, Decimal] = field(default_factory=dict)
    equity_reconciliation_status: str | None = None
    equity_reconciliation_difference: Decimal | None = None
    valuation_errors: tuple[str, ...] = ()
    position_value_observation_difference: Decimal | None = None
    position_value_observation_status: str | None = None
    valuation_observation_times: Mapping[str, str] = field(default_factory=dict)
    accounting_states: Mapping[str, str] = field(default_factory=dict)
    reconciliation_results: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", require_text(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "as_of", require_aware(self.as_of, "as_of"))
        object.__setattr__(self, "total_equity", decimal_value(self.total_equity, "total_equity", nonnegative=True))
        if self.valuation_version not in (1, 2):
            raise ValueError("unknown snapshot valuation version")
        object.__setattr__(self, "cash_balance", decimal_value(self.cash_balance, "cash_balance", nonnegative=self.valuation_version == 1))
        object.__setattr__(self, "holdings_value", decimal_value(self.holdings_value, "holdings_value", nonnegative=self.valuation_version == 1))
        for name in ("holdings_cost_basis", "equity_reconciliation_difference", "position_value_observation_difference"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, decimal_value(getattr(self, name), name))
        for name in ("sector_cost_basis", "broker_equity_components"):
            object.__setattr__(self, name, immutable_metadata({k: decimal_value(v, k) for k, v in getattr(self, name).items()}))
        if self.valuation_version == 2 and self.equity_reconciliation_status not in ("matched", "failed"):
            raise ValueError("marked snapshots require explicit equity reconciliation status")
        if self.position_value_observation_status not in (None, 'unavailable',
                'equal_uncoordinated_observations', 'different_uncoordinated_observations'):
            raise ValueError('unknown position observation status')
        for value in self.valuation_observation_times.values():
            require_aware(datetime.fromisoformat(value), 'valuation_received_at')
        object.__setattr__(self, 'valuation_observation_times', immutable_metadata(self.valuation_observation_times))
        object.__setattr__(self, 'accounting_states', immutable_metadata(self.accounting_states))
        object.__setattr__(self, 'reconciliation_results', immutable_metadata(self.reconciliation_results))
        object.__setattr__(self, "valuation_errors", tuple(self.valuation_errors))
        object.__setattr__(self, "sector_exposure", immutable_metadata(self.sector_exposure))
        for name in ("open_positions", "outstanding_orders", "trades_today"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        object.__setattr__(self, "daily_pnl_pct", decimal_value(self.daily_pnl_pct, "daily_pnl_pct"))
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {sorted(_SOURCES)}")
