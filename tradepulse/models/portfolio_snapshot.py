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

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", require_text(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "as_of", require_aware(self.as_of, "as_of"))
        object.__setattr__(self, "total_equity", decimal_value(self.total_equity, "total_equity", nonnegative=True))
        object.__setattr__(self, "cash_balance", decimal_value(self.cash_balance, "cash_balance", nonnegative=True))
        object.__setattr__(self, "holdings_value", decimal_value(self.holdings_value, "holdings_value", nonnegative=True))
        object.__setattr__(self, "sector_exposure", immutable_metadata(self.sector_exposure))
        for name in ("open_positions", "outstanding_orders", "trades_today"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        object.__setattr__(self, "daily_pnl_pct", decimal_value(self.daily_pnl_pct, "daily_pnl_pct"))
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {sorted(_SOURCES)}")
