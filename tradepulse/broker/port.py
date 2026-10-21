"""Broker port -- the one surface the rest of TradePulse may call on a broker.

AlpacaClient satisfies it today; the SimulatedBroker (Phase A0) satisfies it
too. The neutral aliases keep the Alpaca wire types as the single source of
truth while letting callers stop naming a vendor.

Three identities stay distinct: ``venue`` (appears in asset keys),
the account (``AccountRef``), and ``implementation`` (provenance only, never
part of any key).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from tradepulse.models import AssetClass
from tradepulse.models.market import canonical_venue

from .types import (
    AlpacaAccount,
    AlpacaActivity,
    AlpacaClock,
    AlpacaOptionContract,
    AlpacaOrderRequest,
    AlpacaOrderResponse,
    AlpacaPosition,
    AlpacaRateLimitSnapshot,
    RawBar,
    RawQuote,
)

BrokerAccount = AlpacaAccount
BrokerPosition = AlpacaPosition
BrokerClock = AlpacaClock
OrderRequest = AlpacaOrderRequest
OrderState = AlpacaOrderResponse
BrokerActivity = AlpacaActivity
OptionContract = AlpacaOptionContract
RateLimitSnapshot = AlpacaRateLimitSnapshot

_REF_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@runtime_checkable
class BrokerPort(Protocol):
    venue: str
    implementation: str

    async def get_account(self) -> BrokerAccount: ...

    async def get_positions(self) -> list[BrokerPosition]: ...

    async def get_clock(self) -> BrokerClock: ...

    async def place_order(self, order: OrderRequest) -> OrderState: ...

    async def get_order(self, broker_order_id: str) -> OrderState: ...

    async def get_order_by_client_order_id(self, client_order_id: str) -> OrderState | None: ...

    async def get_open_orders(self) -> list[OrderState]: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def get_activities(
        self, activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100,
        *, after_id: str | None = None, page_evidence: list[Any] | None = None,
    ) -> list[BrokerActivity]: ...

    async def get_latest_quote(
        self, symbol: str, asset_class: AssetClass, feed_override: str | None = None
    ) -> RawQuote: ...

    async def get_latest_option_quotes(self, symbols: list[str]) -> dict[str, RawQuote]: ...

    async def get_options_chain(
        self, underlying_symbol: str, expiration_gte: str, expiration_lte: str
    ) -> list[OptionContract]: ...

    async def get_bars(
        self, symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250
    ) -> list[RawBar]: ...

    def set_market_data_feeds(
        self, *, equity_feed: Literal["iex", "sip"], option_feed: Literal["indicative", "opra"]
    ) -> None: ...

    @property
    def rate_limit_snapshot(self) -> RateLimitSnapshot | None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class AccountRef:
    """Identifies one broker account; both parts become a lease file name.
    The venue is canonicalized, so two spellings of one venue share a lease."""

    venue: str
    account_number: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", canonical_venue(self.venue))
        if not isinstance(self.account_number, str) or not _REF_PART.fullmatch(self.account_number):
            raise ValueError(f"invalid account reference account_number: {self.account_number!r}")

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.account_number}"


async def account_ref(broker: BrokerPort) -> AccountRef:
    account = await broker.get_account()
    number = account.account_number
    if not isinstance(number, str) or not number.strip():
        raise ValueError("broker account has no account_number")
    return AccountRef(broker.venue, number)
