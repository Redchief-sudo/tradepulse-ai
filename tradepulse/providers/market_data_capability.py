"""Resolves which Alpaca market-data feeds this account is actually
entitled to -- Basic (IEX equities, free indicative options) vs. Algo
Trader Plus (SIP equities, OPRA options), or a mix of the two (SIP and
OPRA are independently purchasable, not always bundled). Resolved ONCE per
process invocation, before any scan/monitor work starts, and held fixed
for the rest of that run (see cli.py) -- never silently re-negotiated
mid-session.

Policy for the "auto" probes (applies independently to each feed): only a
DEFINITIVE 403 on the premium feed itself counts as "not entitled" -- the
one case AUTO may act on to select the free feed instead. Authentication
failure (401), rate-limiting (429), server errors, a transport failure, or
a 200 response with no usable quote content are all INDETERMINATE and must
propagate rather than being read as "this account is on Basic" -- AUTO may
downgrade only on a PROVEN lack of entitlement, never because TradePulse
couldn't establish what the account is actually authorized to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.models import AssetClass

_PROBE_EQUITY_SYMBOL = "SPY"
_PROBE_OPTIONS_UNDERLYING = "SPY"
_PROBE_CHAIN_WINDOW_DAYS = 45


@dataclass(frozen=True, slots=True)
class MarketDataCapabilities:
    equity_feed: Literal["iex", "sip"]
    option_feed: Literal["indicative", "opra"]

    @property
    def tier_label(self) -> str:
        """Honest, not forced into a binary -- Plus only when BOTH are the
        premium feed, Basic only when BOTH are the free feed, otherwise an
        explicit mixed description (SIP+OPRA aren't always bundled)."""
        if self.equity_feed == "sip" and self.option_feed == "opra":
            return "algo_trader_plus"
        if self.equity_feed == "iex" and self.option_feed == "indicative":
            return "basic"
        return f"mixed:equity={self.equity_feed},option={self.option_feed}"


class MarketDataCapabilityError(RuntimeError):
    """Raised when ALPACA_MARKET_DATA_TIER=algo_trader_plus is explicitly
    required but the account isn't entitled to SIP and/or OPRA -- a startup
    failure, never a silent downgrade."""


async def _probe_sip(broker: AlpacaClient) -> bool:
    try:
        quote = await broker.get_latest_quote(_PROBE_EQUITY_SYMBOL, AssetClass.EQUITY, feed_override="sip")
    except AlpacaError as exc:
        if exc.status_code == 403:
            return False
        raise  # 401/429/5xx/anything else -- indeterminate, must not be read as "Basic"
    if quote.bid is None or quote.ask is None:
        raise MarketDataCapabilityError(
            f"SIP entitlement probe for {_PROBE_EQUITY_SYMBOL} returned no usable quote -- indeterminate, cannot resolve capabilities."
        )
    return True


async def _probe_opra(broker: AlpacaClient) -> bool:
    now = datetime.now(UTC).date()
    gte = now.isoformat()
    lte = (now + timedelta(days=_PROBE_CHAIN_WINDOW_DAYS)).isoformat()
    chain = await broker.get_options_chain(_PROBE_OPTIONS_UNDERLYING, gte, lte)
    if not chain:
        raise MarketDataCapabilityError(
            f"No options chain returned for {_PROBE_OPTIONS_UNDERLYING} -- cannot probe OPRA entitlement."
        )
    probe_contract = chain[0]
    try:
        quote = await broker.get_latest_quote(probe_contract.occ_symbol, AssetClass.OPTION, feed_override="opra")
    except AlpacaError as exc:
        if exc.status_code == 403:
            return False
        raise
    if quote.bid is None or quote.ask is None:
        raise MarketDataCapabilityError(
            f"OPRA entitlement probe for {probe_contract.occ_symbol} returned no usable quote -- indeterminate, cannot resolve capabilities."
        )
    return True


async def resolve_market_data_capabilities(broker: AlpacaClient, requested_tier: str) -> MarketDataCapabilities:
    if requested_tier == "basic":
        return MarketDataCapabilities("iex", "indicative")  # explicit no-probe override

    equity_entitled = await _probe_sip(broker)
    option_entitled = await _probe_opra(broker)

    if requested_tier == "algo_trader_plus":
        missing = [name for name, ok in (("SIP", equity_entitled), ("OPRA", option_entitled)) if not ok]
        if missing:
            raise MarketDataCapabilityError(
                f"ALPACA_MARKET_DATA_TIER=algo_trader_plus requires SIP and OPRA entitlement, "
                f"but this account is not authorized for: {', '.join(missing)}."
            )
        return MarketDataCapabilities("sip", "opra")

    # "auto"
    return MarketDataCapabilities(
        "sip" if equity_entitled else "iex",
        "opra" if option_entitled else "indicative",
    )
