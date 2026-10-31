"""DBAE capability evidence (spec §3.1, §3.2, §5.1, §6).

Separate protocols, not BrokerPort methods (DBAE-03). Every field is
``T | None`` and None means unknown: unparsable values, wrong types and
non-finite decimals parse to None, never to a permissive value. A request
failure raises the client's AlpacaError; the gateway treats it as unknown
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from tradepulse.calendar import ny_date
from tradepulse.models import AssetClass
from tradepulse.models.base import DomainValidationError, decimal_value
from tradepulse.models.market import AssetIdentity

from .alpaca_client import AlpacaClient
from .errors import AlpacaDataIntegrityError, AlpacaError, raise_alpaca_error

_FRESH_PAST = timedelta(seconds=30)
_FRESH_FUTURE = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    status: str | None
    trading_blocked: bool | None
    account_blocked: bool | None
    trade_suspended_by_user: bool | None
    suspend_trade: bool | None
    closing_transactions_only: bool | None
    fractional_trading: bool | None
    equity: Decimal | None
    cash: Decimal | None
    buying_power: Decimal | None
    non_marginable_buying_power: Decimal | None
    account_multiplier: Decimal | None
    crypto_status: str | None
    options_trading_level: int | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class AssetTradingCapabilities:
    active: bool | None
    tradable: bool | None
    fractionable: bool | None
    min_order_size: Decimal | None
    min_trade_increment: Decimal | None
    price_increment: Decimal | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DailyEquityBar:
    trading_date: date
    equity: Decimal


@runtime_checkable
class AccountCapabilities(Protocol):
    async def get_account_evidence(self) -> AccountEvidence: ...


@runtime_checkable
class AssetCapabilities(Protocol):
    async def get_asset_capabilities(self, asset: AssetIdentity) -> AssetTradingCapabilities: ...


@runtime_checkable
class PortfolioHistory(Protocol):
    async def get_daily_equity_bars(self) -> list[DailyEquityBar]: ...


@runtime_checkable
class DbaeCapabilities(AccountCapabilities, AssetCapabilities, PortfolioHistory, Protocol):
    """The single type the DBAE gateway depends on."""


def evidence_fresh(received_at: datetime, now: datetime) -> bool:
    return now - _FRESH_PAST <= received_at <= now + _FRESH_FUTURE


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return decimal_value(value, "capability_decimal")
    except DomainValidationError:
        return None


def _level(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _object(response_body: object, operation: str) -> dict[str, Any]:
    if not isinstance(response_body, dict):
        raise AlpacaDataIntegrityError(f"{operation}: expected an object")
    return response_body


class AlpacaCapabilities:
    """Reads account, asset and portfolio-history evidence from Alpaca."""

    def __init__(self, client: AlpacaClient) -> None:
        self._client = client

    async def _get(self, path: str, operation: str, **params: str) -> dict[str, Any]:
        response = await self._client._request("GET", f"{self._client._trading_base}{path}", params=params or None)
        if not response.is_success:
            raise_alpaca_error(response, operation)
        return _object(response.json(), operation)

    async def get_account_evidence(self) -> AccountEvidence:
        account = await self._get("/account", "getAccount")
        config = await self._get("/account/configurations", "getAccountConfigurations")
        return AccountEvidence(
            status=_text(account.get("status")),
            trading_blocked=_flag(account.get("trading_blocked")),
            account_blocked=_flag(account.get("account_blocked")),
            trade_suspended_by_user=_flag(account.get("trade_suspended_by_user")),
            suspend_trade=_flag(config.get("suspend_trade")),
            closing_transactions_only=_flag(config.get("closing_transactions_only")),
            fractional_trading=_flag(config.get("fractional_trading")),
            equity=_decimal(account.get("equity")),
            cash=_decimal(account.get("cash")),
            buying_power=_decimal(account.get("buying_power")),
            non_marginable_buying_power=_decimal(account.get("non_marginable_buying_power")),
            account_multiplier=_decimal(account.get("multiplier")),
            crypto_status=_text(account.get("crypto_status")),
            options_trading_level=_level(account.get("options_trading_level")),
            received_at=datetime.now(UTC),
        )

    async def get_asset_capabilities(self, asset: AssetIdentity) -> AssetTradingCapabilities:
        if asset.asset_class is AssetClass.OPTION:
            body = await self._get(f"/options/contracts/{quote(asset.symbol, safe='')}", "getOptionContract")
            status = _text(body.get("status"))
            # Options trade as whole contracts: no fraction, no minimum size or increment.
            return AssetTradingCapabilities(
                active=None if status is None else status == "active", tradable=_flag(body.get("tradable")),
                fractionable=False, min_order_size=None, min_trade_increment=None, price_increment=None,
                received_at=datetime.now(UTC),
            )
        body = await self._get(f"/assets/{quote(asset.symbol, safe='')}", "getAsset")
        status = _text(body.get("status"))
        return AssetTradingCapabilities(
            active=None if status is None else status == "active", tradable=_flag(body.get("tradable")),
            fractionable=_flag(body.get("fractionable")),
            min_order_size=_decimal(body.get("min_order_size")),
            min_trade_increment=_decimal(body.get("min_trade_increment")),
            price_increment=_decimal(body.get("price_increment")),
            received_at=datetime.now(UTC),
        )

    async def get_daily_equity_bars(self) -> list[DailyEquityBar]:
        body = await self._get("/account/portfolio/history", "getPortfolioHistory", period="1M", timeframe="1D")
        stamps, equities = body.get("timestamp"), body.get("equity")
        if not isinstance(stamps, list) or not isinstance(equities, list) or len(stamps) != len(equities):
            raise AlpacaDataIntegrityError("getPortfolioHistory: timestamp and equity must be lists of equal length")
        bars: list[DailyEquityBar] = []
        for stamp, raw_equity in zip(stamps, equities, strict=True):
            equity = _decimal(raw_equity)
            if isinstance(stamp, bool) or not isinstance(stamp, int) or equity is None or equity < 0:
                continue
            try:
                trading_date = ny_date(datetime.fromtimestamp(stamp, UTC))
            except (OverflowError, OSError, ValueError):
                continue
            bars.append(DailyEquityBar(trading_date=trading_date, equity=equity))
        return sorted(bars, key=lambda bar: bar.trading_date)




class NoCapabilityEvidence:
    """The evidence source of a broker that has none: every read fails as a
    broker read would, so every new entry is refused as unknown evidence
    (spec §3.2, §10). Protective exits never read it."""

    async def get_account_evidence(self) -> AccountEvidence:
        raise AlpacaError("no DBAE evidence source for this broker", 503, None, None, "getAccount")

    async def get_asset_capabilities(self, asset: AssetIdentity) -> AssetTradingCapabilities:
        raise AlpacaError("no DBAE evidence source for this broker", 503, None, None, "getAsset")

    async def get_daily_equity_bars(self) -> list[DailyEquityBar]:
        raise AlpacaError("no DBAE evidence source for this broker", 503, None, None, "getPortfolioHistory")


def capabilities_for(broker: object) -> DbaeCapabilities:
    """The DBAE evidence source for ``broker``: Alpaca's own endpoints for the
    Alpaca client, and NoCapabilityEvidence for any other broker. Tests give
    a simulated broker's gateway its SimulatedCapabilities directly; no
    production module imports the simulator (R12)."""
    if isinstance(broker, AlpacaClient):
        return AlpacaCapabilities(broker)
    return NoCapabilityEvidence()
