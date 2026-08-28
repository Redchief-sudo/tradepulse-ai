"""Async Alpaca REST client -- port of base44/shared/alpaca.ts.

Paper vs live differ ONLY in the trading-API base URL; every other code path
is identical, matching the audited principle that paper/live divergence must
stay minimal and explicit.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

import httpx

from tradepulse.models import AssetClass, Side

from .errors import AlpacaDataIntegrityError, extract_request_id, raise_alpaca_error
from .symbols import infer_alpaca_asset_class, normalize_alpaca_symbol
from .types import (
    AlpacaAccount,
    AlpacaActivity,
    AlpacaClock,
    AlpacaOptionContract,
    AlpacaOrderRequest,
    AlpacaOrderResponse,
    AlpacaPosition,
    RawBar,
    RawQuote,
)

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2"
LIVE_TRADING_BASE = "https://api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.replace("Z", "+00:00")
    value = _FRACTION_RE.sub(r"\1", value)
    return datetime.fromisoformat(value)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class AlpacaClient:
    def __init__(
        self, api_key: str, api_secret: str, mode: Literal["paper", "live"], timeout_seconds: int,
        equity_feed: Literal["iex", "sip"] = "iex", option_feed: Literal["indicative", "opra"] = "indicative",
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._mode = mode
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        # Defaults are the always-working Basic tier -- a safe fail-safe
        # for any caller that never resolves capabilities (see
        # providers/market_data_capability.py). set_market_data_feeds is
        # meant to be called ONCE, right after construction, before any
        # market-data work starts -- never mid-session.
        self._equity_feed = equity_feed
        self._option_feed = option_feed

    def set_market_data_feeds(self, *, equity_feed: Literal["iex", "sip"], option_feed: Literal["indicative", "opra"]) -> None:
        self._equity_feed = equity_feed
        self._option_feed = option_feed

    @property
    def _trading_base(self) -> str:
        return LIVE_TRADING_BASE if self._mode == "live" else PAPER_TRADING_BASE

    @property
    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._api_key, "APCA-API-SECRET-KEY": self._api_secret}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AlpacaClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def get_clock(self) -> AlpacaClock:
        response = await self._client.get(f"{self._trading_base}/clock", headers=self._headers)
        if not response.is_success:
            raise_alpaca_error(response, "getClock")
        data = response.json()
        return AlpacaClock(
            is_open=bool(data.get("is_open")),
            next_open=_parse_timestamp(data.get("next_open")),
            next_close=_parse_timestamp(data.get("next_close")),
            timestamp=_parse_timestamp(data.get("timestamp")),
        )

    async def get_account(self) -> AlpacaAccount:
        response = await self._client.get(f"{self._trading_base}/account", headers=self._headers)
        if not response.is_success:
            raise_alpaca_error(response, "getAccount")
        data = response.json()
        return AlpacaAccount(
            equity=_decimal(data.get("equity", "0")),
            last_equity=_decimal(data.get("last_equity", "0")),
            cash=_decimal(data.get("cash", "0")),
            buying_power=_decimal(data.get("buying_power", "0")),
            portfolio_value=_decimal(data.get("portfolio_value", "0")),
        )

    async def get_positions(self) -> list[AlpacaPosition]:
        response = await self._client.get(f"{self._trading_base}/positions", headers=self._headers)
        if not response.is_success:
            raise_alpaca_error(response, "getPositions")
        data = response.json()
        rows = data if isinstance(data, list) else []
        positions: list[AlpacaPosition] = []
        for row in rows:
            raw_class = row.get("asset_class")
            # Verified live against Alpaca's docs 2026-08-26: "crypto",
            # "us_option", "us_equity" are the three values this field
            # actually takes. Anything else fails loudly rather than being
            # silently coerced into EQUITY -- an unrecognized asset_class
            # here would otherwise build a WRONG canonical identity for a
            # real broker position, corrupting Holding lookups, protective-
            # exit classification, and reconciliation with no signal it
            # happened.
            if raw_class == "crypto":
                asset_class = AssetClass.CRYPTO
            elif raw_class == "us_option":
                asset_class = AssetClass.OPTION
            elif raw_class == "us_equity":
                asset_class = AssetClass.EQUITY
            else:
                raise AlpacaDataIntegrityError(
                    f"BROKER_ASSET_CLASS_UNKNOWN: position {row.get('symbol')!r} has unrecognized asset_class {raw_class!r}"
                )
            positions.append(
                AlpacaPosition(
                    symbol=normalize_alpaca_symbol(str(row.get("symbol", "")), asset_class),
                    asset_class=asset_class,
                    qty=_decimal(row.get("qty", "0")),
                    avg_entry_price=_decimal(row.get("avg_entry_price", "0")),
                    market_value=_decimal(row.get("market_value", "0")),
                    current_price=_decimal(row.get("current_price", "0")),
                    unrealized_pl=_decimal(row.get("unrealized_pl", "0")),
                )
            )
        return positions

    async def get_latest_quote(self, symbol: str, asset_class: AssetClass, feed_override: str | None = None) -> RawQuote:
        """feed_override forces a specific feed for this ONE call without
        mutating client state -- used only by
        providers/market_data_capability.py's entitlement probes. Every
        ordinary caller (scanner, gateway, settlement) omits it and gets
        whatever set_market_data_feeds last resolved (Basic's iex/
        indicative by default, until a capability resolution runs)."""
        normalized = str(symbol).upper().replace("-", "/")
        is_crypto = asset_class == AssetClass.CRYPTO
        is_option = asset_class == AssetClass.OPTION
        if is_crypto:
            url = f"{DATA_BASE}/v1beta3/crypto/us/latest/quotes"
            response = await self._client.get(url, headers=self._headers, params={"symbols": normalized})
        elif is_option:
            # Verified live against Alpaca's docs 2026-08-26: same
            # {"quotes": {symbol: {bp, ap, t}}} envelope shape as crypto's
            # quotes endpoint, keyed by the OCC symbol. feed is always
            # explicit (never Alpaca's own silent per-request default) --
            # resolved once at startup (see providers/
            # market_data_capability.py) and held for the whole session, so
            # a Basic account cleanly uses "indicative" throughout and a
            # Plus account cleanly uses "opra" throughout, each correctly
            # tagged in Opportunity provenance -- never a silent per-request
            # fallback with no signal which feed actually served the quote.
            option_feed = feed_override or self._option_feed
            url = f"{DATA_BASE}/v1beta1/options/quotes/latest"
            response = await self._client.get(url, headers=self._headers, params={"symbols": normalized, "feed": option_feed})
        else:
            equity_feed = feed_override or self._equity_feed
            url = f"{DATA_BASE}/v2/stocks/{normalized}/quotes/latest"
            response = await self._client.get(url, headers=self._headers, params={"feed": equity_feed})
        if not response.is_success:
            raise_alpaca_error(response, "getLatestQuote")
        data = response.json()
        quote = (data.get("quotes") or {}).get(normalized) if (is_crypto or is_option) else data.get("quote")
        quote = quote or {}
        if is_crypto:
            source = "alpaca_crypto"
        elif is_option:
            source = f"alpaca_{feed_override or self._option_feed}"
        else:
            source = f"alpaca_{feed_override or self._equity_feed}"
        return RawQuote(
            symbol=normalized,
            bid=_decimal_or_none(quote.get("bp")),
            ask=_decimal_or_none(quote.get("ap")),
            timestamp=_parse_timestamp(quote.get("t")),
            source=source,
        )

    async def get_options_chain(
        self, underlying_symbol: str, expiration_gte: str, expiration_lte: str
    ) -> list[AlpacaOptionContract]:
        """GET /v2/options/contracts -- contract metadata/eligibility, NOT
        price data, so it lives on the TRADING API host, not DATA_BASE.
        Verified live against Alpaca's docs 2026-08-26. Only active,
        tradable contracts within [expiration_gte, expiration_lte]
        (YYYY-MM-DD) for one underlying -- callers apply their own DTE
        window on top of this (strategy/options_selection.py)."""
        contracts: list[AlpacaOptionContract] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {
                "underlying_symbols": underlying_symbol.upper(), "status": "active",
                "expiration_date_gte": expiration_gte, "expiration_date_lte": expiration_lte,
                "limit": "500",
            }
            if page_token:
                params["page_token"] = page_token
            response = await self._client.get(f"{self._trading_base}/options/contracts", headers=self._headers, params=params)
            if not response.is_success:
                raise_alpaca_error(response, "getOptionsChain")
            data = response.json()
            for row in data.get("option_contracts") or []:
                if not row.get("tradable", True):
                    continue
                contracts.append(
                    AlpacaOptionContract(
                        occ_symbol=str(row.get("symbol", "")).upper(),
                        underlying_symbol=str(row.get("underlying_symbol", "")).upper(),
                        option_type=str(row.get("type", "")).lower(),
                        strike_price=_decimal(row.get("strike_price", "0")),
                        expiration_date=str(row.get("expiration_date", "")),
                        multiplier=_decimal(row.get("multiplier", "100")),
                        status=str(row.get("status", "")),
                        tradable=bool(row.get("tradable", True)),
                    )
                )
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return contracts

    async def get_bars(
        self, symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250
    ) -> list[RawBar]:
        normalized = str(symbol).upper().replace("-", "/")
        is_crypto = asset_class == AssetClass.CRYPTO
        params = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": str(limit),
            # Fetch the most recent `limit` bars (descending), then reverse to
            # ascending below. Crypto trades every calendar day while equities
            # only trade weekdays, so a fixed calendar-day window can contain
            # more crypto bars than `limit` -- requesting ascending order in
            # that case silently truncates at the OLDEST end and returns stale
            # data missing everything since, rather than the most recent
            # history. Confirmed live: BTC/USD candles stopped ~2.5 months
            # before "today" under ascending sort; AAPL (weekdays-only, fewer
            # bars than the window) happened not to hit the same truncation.
            "sort": "desc",
        }
        if is_crypto:
            url = f"{DATA_BASE}/v1beta3/crypto/us/bars"
            response = await self._client.get(url, headers=self._headers, params={**params, "symbols": normalized})
        else:
            url = f"{DATA_BASE}/v2/stocks/{normalized}/bars"
            response = await self._client.get(url, headers=self._headers, params={**params, "feed": self._equity_feed})
        if not response.is_success:
            raise_alpaca_error(response, "getBars")
        data = response.json()
        rows = (data.get("bars") or {}).get(normalized) if is_crypto else (data.get("bars") or [])
        rows = rows or []
        bars: list[RawBar] = []
        for row in rows:
            close = _decimal_or_none(row.get("c"))
            if close is None or close <= 0:
                continue
            timestamp = _parse_timestamp(row.get("t"))
            bars.append(
                RawBar(
                    date=(timestamp.date().isoformat() if timestamp else str(row.get("t"))[:10]),
                    open=_decimal(row.get("o", "0")),
                    high=_decimal(row.get("h", "0")),
                    low=_decimal(row.get("l", "0")),
                    close=close,
                    volume=_decimal(row.get("v", "0")),
                )
            )
        bars.reverse()  # rows arrived most-recent-first (desc); callers expect oldest-first.
        return bars

    async def place_order(self, order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        body: dict[str, object] = {
            "symbol": order.symbol.upper(),
            "qty": str(order.qty),
            "side": order.side.value,
            "type": order.order_type,
            "time_in_force": order.time_in_force,
        }
        if order.client_order_id:
            body["client_order_id"] = order.client_order_id
        if order.order_type in ("limit", "stop_limit") and order.limit_price is not None:
            body["limit_price"] = str(order.limit_price)
        if order.order_type in ("stop", "stop_limit") and order.stop_price is not None:
            body["stop_price"] = str(order.stop_price)
        response = await self._client.post(f"{self._trading_base}/orders", headers=self._headers, json=body)
        if not response.is_success:
            raise_alpaca_error(response, "placeOrder")
        return self._parse_order_response(response)

    async def get_order(self, broker_order_id: str) -> AlpacaOrderResponse:
        response = await self._client.get(f"{self._trading_base}/orders/{broker_order_id}", headers=self._headers)
        if not response.is_success:
            raise_alpaca_error(response, "getOrder")
        return self._parse_order_response(response)

    async def get_order_by_client_order_id(self, client_order_id: str) -> AlpacaOrderResponse | None:
        """Recovery lookup for an ambiguous submission outcome (see
        broker/errors.py::is_definitive_rejection). Returns None only for a
        genuine 404 (Alpaca never received/created this order) -- any other
        non-success response is still an ambiguous outcome and must raise,
        not be silently treated as "not found"."""
        response = await self._client.get(
            f"{self._trading_base}/orders:by_client_order_id",
            headers=self._headers,
            params={"client_order_id": client_order_id},
        )
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise_alpaca_error(response, "getOrderByClientOrderId")
        return self._parse_order_response(response)

    async def cancel_order(self, broker_order_id: str) -> None:
        response = await self._client.delete(f"{self._trading_base}/orders/{broker_order_id}", headers=self._headers)
        if not response.is_success:
            raise_alpaca_error(response, "cancelOrder")

    async def get_activities(
        self, activity_type: str = "FILL", since: datetime | None = None, page_size: int = 100
    ) -> list[AlpacaActivity]:
        activities: list[AlpacaActivity] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {"activity_types": activity_type, "page_size": str(page_size), "direction": "asc"}
            if since is not None:
                params["after"] = since.isoformat()
            if page_token:
                params["page_token"] = page_token
            response = await self._client.get(f"{self._trading_base}/account/activities", headers=self._headers, params=params)
            if not response.is_success:
                raise_alpaca_error(response, "getActivities")
            page = response.json()
            page = page if isinstance(page, list) else []
            for row in page:
                side_raw = str(row.get("side") or "").lower()
                side = Side.BUY if side_raw in ("buy", "buy_to_cover") else Side.SELL if side_raw in ("sell", "sell_short") else None
                raw_symbol = str(row.get("symbol", ""))
                # Activities responses don't carry an asset_class field the
                # way positions do -- infer it from the ticker shape (same
                # helper used wherever a caller only has a bare symbol).
                inferred_class = infer_alpaca_asset_class(raw_symbol)
                activities.append(
                    AlpacaActivity(
                        activity_id=str(row.get("id", "")),
                        activity_type=str(row.get("activity_type", "")),
                        symbol=normalize_alpaca_symbol(raw_symbol, inferred_class),
                        side=side,
                        qty=_decimal_or_none(row.get("qty")),
                        price=_decimal_or_none(row.get("price")),
                        transaction_time=_parse_timestamp(row.get("transaction_time")),
                        raw=row,
                    )
                )
            if len(page) < page_size:
                break
            page_token = str(page[-1].get("id") or "")
            if not page_token:
                break
        return activities

    def _parse_order_response(self, response: httpx.Response) -> AlpacaOrderResponse:
        data = response.json()
        side_raw = str(data.get("side") or "").lower()
        side = Side.BUY if side_raw == "buy" else Side.SELL if side_raw == "sell" else None
        return AlpacaOrderResponse(
            broker_order_id=str(data.get("id", "")),
            status=str(data.get("status", "")),
            symbol=str(data.get("symbol", "")),
            side=side,
            filled_qty=_decimal(data.get("filled_qty") or "0"),
            filled_avg_price=_decimal_or_none(data.get("filled_avg_price")),
            submitted_at=_parse_timestamp(data.get("submitted_at")),
            request_id=extract_request_id(response),
            raw=data,
        )
