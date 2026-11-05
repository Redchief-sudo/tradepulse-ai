"""Authoritative execution-reference pricing -- port of execution.ts's
fetchAuthoritativeQuote/executableQuoteMetrics. The gateway NEVER trusts an
AI/caller-supplied price: it always fetches its own quote and uses that same
price for risk sizing, order metadata, and freshness validation.
"""

from __future__ import annotations

from collections.abc import Callable
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


# Rev.149 F05: the most a quote's own timestamp may run ahead of the local
# clock and still count as fresh (age 0). Beyond it, local time and the
# market data source disagree, and the quote is refused rather than trusted.
QUOTE_CLOCK_SKEW_SECONDS = 2


def quote_age_problem(asset_class: AssetClass, observed_at: datetime, now: datetime) -> str | None:
    """None when a quote observed at ``observed_at`` is fresh at ``now``,
    otherwise the reason. ``now`` must be read after the quote was received:
    the age of evidence is measured when it is used, never from a time taken
    before the awaits that fetched it (Rev.149 F05)."""
    age_seconds = (now - observed_at).total_seconds()
    max_age = max_quote_age_seconds(asset_class)
    if age_seconds < -QUOTE_CLOCK_SKEW_SECONDS:
        return f"quote timestamp {-age_seconds:.0f}s ahead of the local clock exceeds the {QUOTE_CLOCK_SKEW_SECONDS}s skew allowance"
    if age_seconds > max_age:
        return f"quote age {age_seconds:.0f}s exceeds {max_age}s"
    return None


async def fetch_authoritative_quote(
    provider: AlpacaMarketDataProvider, asset: AssetIdentity, side: Side,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AuthoritativeQuote:
    quote = await provider.fetch_quote(asset)  # raises ProviderError on invalid/missing bid-ask -- never fabricates one

    problem = quote_age_problem(asset.asset_class, quote.observed_at, clock())  # measured on receipt (Rev.149 F05)
    if problem is not None:
        raise ProviderDataFailure(
            "alpaca", asset.symbol, "fetch_authoritative_quote", "STALE_EXECUTABLE_QUOTE", problem, retryable=True,
        )

    if quote.bid is None or quote.ask is None:  # the provider refuses these already; never priced from a missing side
        raise ProviderDataFailure(
            "alpaca", asset.symbol, "fetch_authoritative_quote", "ALPACA_QUOTE_INVALID",
            f"invalid bid/ask for {asset.symbol}: bid={quote.bid} ask={quote.ask}", retryable=True,
        )
    mid = (quote.bid + quote.ask) / 2
    reference_price = quote.ask if side == Side.BUY else quote.bid
    estimated_slippage_pct = ((quote.ask - quote.bid) / 2 / mid) * 100 if mid > 0 else Decimal("0")
    return AuthoritativeQuote(
        price=reference_price, bid=quote.bid, ask=quote.ask,
        observed_at=quote.observed_at, estimated_slippage_pct=estimated_slippage_pct,
    )
