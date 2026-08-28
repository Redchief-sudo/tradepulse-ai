"""Alpaca-backed market data -- the sole market-data provider for this MVP
(equities + crypto quotes/candles both routed through Alpaca, per the
Alpaca-only market-data decision). Every failure path raises a typed
ProviderError; none returns fabricated, zero-filled, or interpolated data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tradepulse.broker import AlpacaClient, AlpacaError
from tradepulse.models import AssetIdentity, Candle, MarketQuote
from tradepulse.strategy.options_selection import OptionContractSummary

from .errors import ProviderDataFailure, ProviderHttpFailure

MIN_CANDLES = 30


class AlpacaMarketDataProvider:
    def __init__(self, client: AlpacaClient) -> None:
        self._client = client

    async def fetch_quote(self, asset: AssetIdentity) -> MarketQuote:
        try:
            raw = await self._client.get_latest_quote(asset.symbol, asset.asset_class)
        except AlpacaError as exc:
            raise ProviderHttpFailure("alpaca", asset.symbol, "fetch_quote", exc.status_code, exc.message) from exc

        if raw.bid is None or raw.ask is None or raw.bid <= 0 or raw.ask <= 0 or raw.ask < raw.bid:
            raise ProviderDataFailure(
                "alpaca", asset.symbol, "fetch_quote", "ALPACA_QUOTE_INVALID",
                f"invalid bid/ask for {asset.symbol}: bid={raw.bid} ask={raw.ask}", retryable=True,
            )
        if raw.timestamp is None:
            raise ProviderDataFailure(
                "alpaca", asset.symbol, "fetch_quote", "ALPACA_QUOTE_MISSING_TIMESTAMP",
                f"missing quote timestamp for {asset.symbol}", retryable=True,
            )

        return MarketQuote(
            asset=asset,
            price=(raw.bid + raw.ask) / 2,
            observed_at=raw.timestamp,
            received_at=datetime.now(UTC),
            provider=raw.source,
            market_generation=0,
            bid=raw.bid,
            ask=raw.ask,
        )

    async def fetch_candles(self, asset: AssetIdentity, lookback_days: int = 200) -> list[Candle]:
        end = datetime.now(UTC)
        # Calendar-day buffer for weekends/holidays so `lookback_days` trading
        # sessions actually fit in the requested window.
        start = end - timedelta(days=int(lookback_days * 1.6) + 10)
        try:
            raw_bars = await self._client.get_bars(
                asset.symbol, asset.asset_class, start, end, limit=max(lookback_days + 50, 250)
            )
        except AlpacaError as exc:
            raise ProviderHttpFailure("alpaca", asset.symbol, "fetch_candles", exc.status_code, exc.message) from exc

        if len(raw_bars) < MIN_CANDLES:
            raise ProviderDataFailure(
                "alpaca", asset.symbol, "fetch_candles", "INSUFFICIENT_HISTORY",
                f"{asset.symbol} returned {len(raw_bars)} bars, need >= {MIN_CANDLES}", retryable=False,
            )

        return [
            Candle(date=bar.date, open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume)
            for bar in raw_bars
        ]

    async def fetch_option_chain(
        self, underlying_symbol: str, min_dte: int, max_dte: int, now: date
    ) -> list[OptionContractSummary]:
        """Translates the raw broker chain response into the domain shape
        strategy/options_selection.py::select_contract operates over.
        expiration_gte/lte are computed here from the DTE window so the
        broker only ever returns contracts that could possibly be
        eligible -- select_contract still re-applies the exact window
        itself (this is a server-side prefilter, not a substitute)."""
        gte = date.fromordinal(now.toordinal() + min_dte).isoformat()
        lte = date.fromordinal(now.toordinal() + max_dte).isoformat()
        try:
            raw_contracts = await self._client.get_options_chain(underlying_symbol, gte, lte)
        except AlpacaError as exc:
            raise ProviderHttpFailure("alpaca", underlying_symbol, "fetch_option_chain", exc.status_code, exc.message) from exc

        summaries: list[OptionContractSummary] = []
        for contract in raw_contracts:
            if contract.option_type not in ("call", "put"):
                continue
            try:
                expiry = date.fromisoformat(contract.expiration_date)
            except ValueError:
                continue
            summaries.append(
                OptionContractSummary(
                    occ_symbol=contract.occ_symbol, underlying_symbol=contract.underlying_symbol,
                    option_type=contract.option_type, strike=contract.strike_price, expiry=expiry,
                    contract_multiplier=contract.multiplier,
                )
            )
        return summaries
