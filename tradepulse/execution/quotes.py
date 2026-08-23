"""Authoritative execution-reference pricing -- port of execution.ts's
fetchAuthoritativeQuote/executableQuoteMetrics. The gateway NEVER trusts an
AI/caller-supplied price: it always fetches its own quote and uses that same
price for risk sizing, order metadata, and freshness validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from tradepulse.models import AssetClass, AssetIdentity, Side
from tradepulse.providers import AlpacaMarketDataProvider, ProviderDataFailure


@dataclass(frozen=True, slots=True)
class AuthoritativeQuote:
    price: Decimal  # side-appropriate: ask for a buy, bid for a sell
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    estimated_slippage_pct: Decimal


def max_quote_age_seconds(asset_class: AssetClass) -> int:
    return 60 if asset_class == AssetClass.CRYPTO else 30


async def fetch_authoritative_quote(
    provider: AlpacaMarketDataProvider, asset: AssetIdentity, side: Side, now: datetime | None = None
) -> AuthoritativeQuote:
    now = now or datetime.now(UTC)
    quote = await provider.fetch_quote(asset)  # raises ProviderError on invalid/missing bid-ask -- never fabricates one

    age_seconds = (now - quote.observed_at).total_seconds()
    max_age = max_quote_age_seconds(asset.asset_class)
    if age_seconds < 0 or age_seconds > max_age:
        raise ProviderDataFailure(
            "alpaca", asset.symbol, "fetch_authoritative_quote", "STALE_EXECUTABLE_QUOTE",
            f"quote age {age_seconds:.0f}s exceeds {max_age}s", retryable=True,
        )

    mid = (quote.bid + quote.ask) / 2
    reference_price = quote.ask if side == Side.BUY else quote.bid
    estimated_slippage_pct = ((quote.ask - quote.bid) / 2 / mid) * 100 if mid > 0 else Decimal("0")
    return AuthoritativeQuote(
        price=reference_price, bid=quote.bid, ask=quote.ask,
        observed_at=quote.observed_at, estimated_slippage_pct=estimated_slippage_pct,
    )
