from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .base import decimal_value, immutable_metadata, require_aware, require_text
from .enums import AssetClass


@dataclass(frozen=True, slots=True)
class AssetIdentity:
    symbol: str
    asset_class: AssetClass
    native_asset_id: str
    venue: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.asset_class, AssetClass):
            raise TypeError("asset_class must be AssetClass")
        object.__setattr__(self, "symbol", require_text(self.symbol, "symbol").upper())
        object.__setattr__(self, "native_asset_id", require_text(self.native_asset_id, "native_asset_id"))
        object.__setattr__(self, "metadata", immutable_metadata(self.metadata))


def asset_identity_key(asset: AssetIdentity) -> str:
    """The single definition of financial-instrument equality used
    everywhere local state is keyed or matched -- Holding record IDs, lot
    matching, in-flight-intent checks, idempotency keys, execution
    reservations, reconciliation buckets. Derived from instrument identity
    (asset class + venue + broker-native ID), never from display symbol
    alone -- a ticker-shaped symbol can be shared by economically distinct
    instruments (a crypto pair and an equity today; two option contracts or
    two futures expirations once those asset classes exist). native_asset_id
    is intentionally NOT case-normalized here -- a future venue's native ID
    may be case-sensitive (an on-chain address, an already-cased OCC
    contract ID); normalization instead happens once, at construction time,
    wherever this system builds a broker-derived native_asset_id."""
    venue = (asset.venue or "default").lower()
    return f"{asset.asset_class.value}:{venue}:{asset.native_asset_id}"


def asset_key_from_broker_symbol(asset_class: AssetClass, symbol: str) -> str:
    """Alpaca is this system's only broker today -- every AssetIdentity built
    from a broker-native fact alone (no local record yet) uses
    native_asset_id=f"alpaca:{symbol}" and no venue (see
    scanner/coordinator.py::_asset_from_candidate,
    reconciliation/coordinator.py's broker-only fallback). Recomputing that
    exact convention here, then reusing asset_identity_key, keeps this in the
    one shared definition rather than a second copy of the format string. A
    second broker/venue would need this parameterized -- out of scope while
    there's only one."""
    return asset_identity_key(AssetIdentity(symbol=symbol, asset_class=asset_class, native_asset_id=f"alpaca:{symbol.upper()}"))


@dataclass(frozen=True, slots=True)
class MarketQuote:
    asset: AssetIdentity
    price: Decimal
    observed_at: datetime
    received_at: datetime
    provider: str
    market_generation: int
    bid: Decimal | None = None
    ask: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal_value(self.price, "price", positive=True))
        object.__setattr__(self, "observed_at", require_aware(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at", require_aware(self.received_at, "received_at"))
        object.__setattr__(self, "provider", require_text(self.provider, "provider"))
        if self.market_generation < 0:
            raise ValueError("market_generation must be nonnegative")
        if self.bid is not None:
            object.__setattr__(self, "bid", decimal_value(self.bid, "bid", positive=True))
        if self.ask is not None:
            object.__setattr__(self, "ask", decimal_value(self.ask, "ask", positive=True))
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError("ask cannot be below bid")

    def is_fresh(self, now: datetime, maximum_age_seconds: int) -> bool:
        current = require_aware(now, "now")
        return 0 <= (current - self.observed_at).total_seconds() <= maximum_age_seconds


@dataclass(frozen=True, slots=True)
class Candle:
    date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", require_text(self.date, "date"))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, positive=True))
        object.__setattr__(self, "volume", decimal_value(self.volume, "volume", nonnegative=True))
        if self.high < self.low:
            raise ValueError("high cannot be below low")
