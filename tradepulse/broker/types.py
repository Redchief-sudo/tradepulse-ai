"""Broker-response DTOs -- thin, unvalidated transport shapes for raw Alpaca
API responses. These are translated into tradepulse.models types (which DO
carry full domain validation) by the execution/settlement layers in later
phases; they intentionally do not use models/base.py's validation helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from tradepulse.models import AssetClass, Side


@dataclass(frozen=True, slots=True)
class AlpacaClock:
    is_open: bool
    next_open: datetime | None
    next_close: datetime | None
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class AlpacaAccount:
    equity: Decimal
    last_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal


@dataclass(frozen=True, slots=True)
class AlpacaPosition:
    symbol: str
    asset_class: AssetClass
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    current_price: Decimal
    unrealized_pl: Decimal


@dataclass(frozen=True, slots=True)
class RawQuote:
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    timestamp: datetime | None
    source: str


@dataclass(frozen=True, slots=True)
class RawBar:
    date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class AlpacaOrderRequest:
    symbol: str
    qty: Decimal
    side: Side
    order_type: str = "market"
    time_in_force: str = "day"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class AlpacaOrderResponse:
    broker_order_id: str
    status: str
    symbol: str
    side: Side | None
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    submitted_at: datetime | None
    request_id: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlpacaActivity:
    activity_id: str
    activity_type: str
    symbol: str
    side: Side | None
    qty: Decimal | None
    price: Decimal | None
    transaction_time: datetime | None
    raw: Mapping[str, Any] = field(default_factory=dict)
