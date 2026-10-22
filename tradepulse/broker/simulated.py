"""SimulatedBroker -- an in-memory BrokerPort that models Alpaca's contract.

Phase A0, Rev.133: the foundation. Time comes only from a ManualClock, the
trading calendar from a SimSession (a real exchange calendar or an explicit
one), and market data from scripted time series that are always read as of a
point in time, never ahead of it. The account is cash plus marked positions,
replayed from a timestamped history so equity at any past instant (and so
``last_equity``) is computed, never stored by a call.

Order execution, Alpaca-exact activities and fee scenarios arrive in Rev.134.
Until then the order book is truthfully empty: reads report no orders and no
activities, and ``place_order`` refuses rather than inventing a fill.

Assumptions that are not verified Alpaca behaviour are named here:

- [scenario] a position's mark is the mid of the latest quote at or before the
  valuation time (Alpaca marks positions at the last trade price);
- [scenario] ``buying_power`` equals cash (a cash account, no margin);
- [scenario] a daily bar is stamped at New York midnight of its date, and is
  visible once that date's session has closed (crypto, or an always-open
  session: once the New York day has ended). An equity or option bar dated on
  a day the calendar has no session for is rejected when scripted;
- quotes with bid above ask are rejected unless scripted with
  ``allow_crossed=True`` (abnormal market data, for fault tests);
- [scenario] before any session close falls after the simulator started,
  ``last_equity`` is the starting equity.

It can never be built from Settings or build_broker, and refuses construction
when TRADEPULSE_EXECUTION_MODE is live.
"""

from __future__ import annotations

import bisect
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from zoneinfo import ZoneInfo

from tradepulse.models import AssetClass
from tradepulse.models.market import canonical_venue

from .errors import AlpacaError
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

NEW_YORK = ZoneInfo("America/New_York")
_ALPACA_ASSET_CLASS = {AssetClass.EQUITY: "us_equity", AssetClass.CRYPTO: "crypto", AssetClass.OPTION: "us_option"}


class SimulatorScriptError(RuntimeError):
    """The scenario script is missing something a real broker would always
    have (for example a price for a held position). Raised instead of
    inventing the missing value."""


def _aware_utc(at: datetime, name: str) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware: {at!r}")
    return at.astimezone(UTC)


def _decimal(value: object, name: str, *, minimum: Decimal | None = None, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal: {value!r}")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive: {value}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}: {value}")
    return value


def _quote_symbol(symbol: str) -> str:
    """AlpacaClient.get_latest_quote's normalization, for every asset class."""
    return str(symbol).upper().replace("-", "/")


class ManualClock:
    """The simulator's only source of time. It moves only when told to, and
    never backwards."""

    def __init__(self, start: datetime) -> None:
        self._now = _aware_utc(start, "start")

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError(f"the clock never moves backwards: {delta}")
        self._now += delta

    def set(self, at: datetime) -> None:
        at = _aware_utc(at, "at")
        if at < self._now:
            raise ValueError(f"the clock never moves backwards: {at} < {self._now}")
        self._now = at


@dataclass(frozen=True)
class SimSession:
    """Regular trading sessions as ordered, non-overlapping (open, close)
    pairs in UTC. Open is inclusive, close exclusive. Beyond the last known
    session, next_open and next_close answer None rather than guess."""

    sessions: tuple[tuple[datetime, datetime], ...]
    always: bool = False

    def __post_init__(self) -> None:
        previous_close: datetime | None = None
        normalized = []
        for open_at, close_at in self.sessions:
            open_at, close_at = _aware_utc(open_at, "open"), _aware_utc(close_at, "close")
            if close_at <= open_at:
                raise ValueError(f"session closes before it opens: {open_at} .. {close_at}")
            if previous_close is not None and open_at < previous_close:
                raise ValueError(f"sessions overlap or are out of order at {open_at}")
            previous_close = close_at
            normalized.append((open_at, close_at))
        object.__setattr__(self, "sessions", tuple(normalized))
        if self.always and self.sessions:
            raise ValueError("an always-open session has no explicit sessions")

    @classmethod
    def explicit(cls, sessions: Iterable[tuple[datetime, datetime]]) -> SimSession:
        return cls(tuple(sessions))

    @classmethod
    def always_open(cls) -> SimSession:
        return cls((), always=True)

    @classmethod
    def from_exchange_calendar(cls, code: str, start: date, end: date) -> SimSession:
        """Sessions from exchange_calendars (a dev-only dependency, imported
        here so production never needs it), including holidays, early closes
        and daylight-saving changes."""
        import exchange_calendars

        calendar = exchange_calendars.get_calendar(code, start=start.isoformat(), end=end.isoformat())
        schedule = calendar.schedule
        pairs = [
            (row.open.to_pydatetime(), row.close.to_pydatetime())
            for row in schedule.itertuples()
        ]
        return cls(tuple(pairs))

    def _opens(self) -> list[datetime]:
        return [open_at for open_at, _ in self.sessions]

    def is_open(self, at: datetime) -> bool:
        at = _aware_utc(at, "at")
        if self.always:
            return True
        index = bisect.bisect_right(self._opens(), at) - 1
        return index >= 0 and at < self.sessions[index][1]

    def next_open(self, at: datetime) -> datetime | None:
        at = _aware_utc(at, "at")
        opens = self._opens()
        index = bisect.bisect_right(opens, at)
        return None if self.always or index >= len(opens) else opens[index]

    def next_close(self, at: datetime) -> datetime | None:
        at = _aware_utc(at, "at")
        closes = [close_at for _, close_at in self.sessions]
        index = bisect.bisect_right(closes, at)
        return None if self.always or index >= len(closes) else closes[index]

    def last_close(self, at: datetime) -> datetime | None:
        """The most recent session close at or before ``at``."""
        at = _aware_utc(at, "at")
        closes = [close_at for _, close_at in self.sessions]
        index = bisect.bisect_right(closes, at) - 1
        return None if index < 0 else closes[index]

    def close_on(self, day: date) -> datetime | None:
        """The close of the session on New York date ``day``, if there is one."""
        for open_at, close_at in self.sessions:
            if open_at.astimezone(NEW_YORK).date() == day:
                return close_at
        return None

    @staticmethod
    def trade_date(at: datetime) -> date:
        return _aware_utc(at, "at").astimezone(NEW_YORK).date()


@dataclass(frozen=True, slots=True)
class _ScriptedQuote:
    at: datetime
    bid: Decimal
    ask: Decimal


@dataclass(frozen=True, slots=True)
class _Holding:
    asset_class: AssetClass
    qty: Decimal
    avg_entry_price: Decimal
    multiplier: Decimal


@dataclass(frozen=True, slots=True)
class _State:
    at: datetime
    cash: Decimal
    holdings: tuple[tuple[str, _Holding], ...]


class SimulatedBroker:
    """An in-memory BrokerPort. See the module docstring for what it models."""

    def __init__(
        self, *, clock: ManualClock, session: SimSession, venue: str = "alpaca", implementation: str = "simulated",
        cash: Decimal = Decimal("100000"), account_number: str = "SIM0001",
    ) -> None:
        if os.environ.get("TRADEPULSE_EXECUTION_MODE", "").strip().lower() == "live":
            raise RuntimeError("SimulatedBroker refuses to exist when TRADEPULSE_EXECUTION_MODE is live")
        self.venue = canonical_venue(venue)
        self.implementation = implementation
        self._clock = clock
        self._session = session
        self._account_number = account_number
        self._account_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"tradepulse-sim:{account_number}"))
        self._created_at = clock()
        self._history: list[_State] = [_State(clock(), _decimal(cash, "cash", minimum=Decimal(0)), ())]
        self._quotes: dict[tuple[str, AssetClass], list[_ScriptedQuote]] = {}
        self._bars: dict[tuple[str, AssetClass], tuple[RawBar, ...]] = {}
        self._chains: dict[str, tuple[AlpacaOptionContract, ...]] = {}
        self._equity_feed: Literal["iex", "sip"] = "iex"
        self._option_feed: Literal["indicative", "opra"] = "indicative"
        self._closed = False
        self.last_positions_received_at: datetime | None = None

    # --- lifecycle -----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SimulatedBroker is closed")

    async def aclose(self) -> None:
        self._closed = True

    @property
    def rate_limit_snapshot(self) -> AlpacaRateLimitSnapshot | None:
        return None  # no HTTP, so no rate-limit headers are ever observed

    def set_market_data_feeds(
        self, *, equity_feed: Literal["iex", "sip"], option_feed: Literal["indicative", "opra"]
    ) -> None:
        self._equity_feed = equity_feed
        self._option_feed = option_feed

    # --- scripting -----------------------------------------------------------

    def set_quote(
        self, symbol: str, asset_class: AssetClass, bid: Decimal, ask: Decimal, at: datetime,
        *, allow_crossed: bool = False,
    ) -> None:
        """Append one quote to the symbol's time series. Times strictly increase.
        A crossed quote (bid above ask) is rejected unless the scenario asks
        for abnormal market data with ``allow_crossed=True``."""
        quote = _ScriptedQuote(_aware_utc(at, "at"), _decimal(bid, "bid", minimum=Decimal(0)),
                               _decimal(ask, "ask", minimum=Decimal(0)))
        if quote.bid > quote.ask and not allow_crossed:
            raise ValueError(f"crossed quote for {symbol}: bid {quote.bid} > ask {quote.ask}")
        series = self._quotes.setdefault((_quote_symbol(symbol), asset_class), [])
        if series and quote.at <= series[-1].at:
            raise ValueError(f"quote times must strictly increase: {quote.at} after {series[-1].at}")
        series.append(quote)

    def set_bars(self, symbol: str, asset_class: AssetClass, bars: Sequence[RawBar]) -> None:
        """Replace the symbol's daily bars. Dates strictly increase; closes are
        positive. An equity or option bar must fall on a session the calendar
        knows (widen the calendar to script older history)."""
        previous: str | None = None
        for bar in bars:
            day = date.fromisoformat(bar.date)
            if (asset_class != AssetClass.CRYPTO and not self._session.always
                    and self._session.close_on(day) is None):
                raise ValueError(f"{symbol} bar {bar.date} is not a session in the configured calendar")
            for name in ("open", "high", "low", "close", "volume"):
                _decimal(getattr(bar, name), f"bar {name}", minimum=Decimal(0))
            if bar.close <= 0:
                raise ValueError(f"bar close must be positive: {bar}")
            if previous is not None and bar.date <= previous:
                raise ValueError(f"bar dates must strictly increase: {bar.date} after {previous}")
            previous = bar.date
        self._bars[(_quote_symbol(symbol), asset_class)] = tuple(bars)

    def set_option_chain(self, underlying_symbol: str, contracts: Sequence[AlpacaOptionContract]) -> None:
        underlying = underlying_symbol.upper()
        for contract in contracts:
            if contract.underlying_symbol.upper() != underlying:
                raise ValueError(f"{contract.occ_symbol} belongs to {contract.underlying_symbol}, not {underlying}")
            date.fromisoformat(contract.expiration_date)
        self._chains[underlying] = tuple(contracts)

    def seed_position(
        self, symbol: str, asset_class: AssetClass, qty: Decimal, avg_entry_price: Decimal,
        *, multiplier: Decimal | None = None,
    ) -> None:
        """[scenario] An inherited position, present from the current clock
        time on, as if transferred in. Options need an explicit multiplier."""
        _decimal(qty, "qty")
        if qty == 0:
            raise ValueError("a seeded position needs a non-zero quantity")
        _decimal(avg_entry_price, "avg_entry_price", positive=True)
        if multiplier is None:
            if asset_class == AssetClass.OPTION:
                raise ValueError("an option position needs an explicit multiplier")
            multiplier = Decimal(1)
        _decimal(multiplier, "multiplier", positive=True)
        key = _quote_symbol(symbol) if asset_class != AssetClass.OPTION else symbol.upper()
        state = self._state_at(self._clock())
        if any(held == key for held, _ in state.holdings):
            raise ValueError(f"{key} is already held")
        holdings = (*state.holdings, (key, _Holding(asset_class, qty, avg_entry_price, multiplier)))
        self._record(_State(self._clock(), state.cash, tuple(sorted(holdings, key=lambda item: item[0]))))

    # --- state ---------------------------------------------------------------

    def _record(self, state: _State) -> None:
        if state.at == self._history[-1].at:
            self._history[-1] = state
        else:
            self._history.append(state)

    def _state_at(self, at: datetime) -> _State:
        index = bisect.bisect_right([state.at for state in self._history], at) - 1
        return self._history[max(index, 0)]

    def quote_at(self, symbol: str, asset_class: AssetClass, at: datetime) -> RawQuote:
        """The latest scripted quote at or before ``at``; empty if there is none."""
        normalized = _quote_symbol(symbol)
        series = self._quotes.get((normalized, asset_class), [])
        index = bisect.bisect_right([quote.at for quote in series], _aware_utc(at, "at")) - 1
        source = self._source(asset_class, None)
        if index < 0:
            return RawQuote(symbol=normalized, bid=None, ask=None, timestamp=None, source=source)
        quote = series[index]
        return RawQuote(symbol=normalized, bid=quote.bid, ask=quote.ask, timestamp=quote.at, source=source)

    def _source(self, asset_class: AssetClass, feed_override: str | None) -> str:
        """Matches AlpacaClient's RawQuote.source, which the scanner maps to a
        market-data authority."""
        if asset_class == AssetClass.CRYPTO:
            return "alpaca_crypto"
        if asset_class == AssetClass.OPTION:
            return f"alpaca_{feed_override or self._option_feed}"
        return f"alpaca_{feed_override or self._equity_feed}"

    def _mark(self, symbol: str, holding: _Holding, at: datetime) -> Decimal:
        quote = self.quote_at(symbol, holding.asset_class, at)
        if quote.bid is None or quote.ask is None:
            raise SimulatorScriptError(f"{symbol} is held but has no quote at or before {at.isoformat()}")
        return (quote.bid + quote.ask) / 2

    def _equity_at(self, at: datetime) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(equity, cash, long market value, short market value) at ``at``."""
        state = self._state_at(at)
        long_value = short_value = Decimal(0)
        for symbol, holding in state.holdings:
            value = holding.qty * self._mark(symbol, holding, at) * holding.multiplier
            if value >= 0:
                long_value += value
            else:
                short_value += value
        return state.cash + long_value + short_value, state.cash, long_value, short_value

    # --- BrokerPort: account, positions, clock --------------------------------

    async def get_clock(self) -> AlpacaClock:
        self._check_open()
        now = self._clock()
        return AlpacaClock(is_open=self._session.is_open(now), next_open=self._session.next_open(now),
                           next_close=self._session.next_close(now), timestamp=now)

    async def get_account(self) -> AlpacaAccount:
        self._check_open()
        now = self._clock()
        equity, cash, long_value, short_value = self._equity_at(now)
        last_close = self._session.last_close(now)
        reference = last_close if last_close is not None and last_close >= self._created_at else self._created_at
        last_equity = self._equity_at(reference)[0]
        raw: dict[str, Any] = {
            "id": self._account_id, "account_number": self._account_number, "status": "ACTIVE", "currency": "USD",
            "cash": str(cash), "equity": str(equity), "last_equity": str(last_equity), "buying_power": str(cash),
            "portfolio_value": str(equity), "long_market_value": str(long_value),
            "short_market_value": str(short_value),
        }
        return AlpacaAccount(
            equity=equity, last_equity=last_equity, cash=cash, buying_power=cash, portfolio_value=equity,
            equity_components={"long_market_value": long_value, "short_market_value": short_value},
            received_at=now, account_id=self._account_id, account_number=self._account_number, raw=raw,
        )

    async def get_positions(self) -> list[AlpacaPosition]:
        self._check_open()
        self.last_positions_received_at = None
        now = self._clock()
        positions = []
        for symbol, holding in self._state_at(now).holdings:
            mark = self._mark(symbol, holding, now)
            market_value = holding.qty * mark * holding.multiplier
            cost_basis = holding.qty * holding.avg_entry_price * holding.multiplier
            raw = {
                "symbol": symbol, "asset_class": _ALPACA_ASSET_CLASS[holding.asset_class], "qty": str(holding.qty),
                "side": "long" if holding.qty > 0 else "short", "avg_entry_price": str(holding.avg_entry_price),
                "market_value": str(market_value), "cost_basis": str(cost_basis), "current_price": str(mark),
                "unrealized_pl": str(market_value - cost_basis),
            }
            positions.append(AlpacaPosition(
                symbol=symbol, asset_class=holding.asset_class, qty=holding.qty,
                avg_entry_price=holding.avg_entry_price, market_value=market_value, current_price=mark,
                unrealized_pl=market_value - cost_basis, cost_basis=cost_basis, received_at=now, raw=raw,
            ))
        self.last_positions_received_at = now
        return positions

    # --- BrokerPort: market data ---------------------------------------------

    async def get_latest_quote(
        self, symbol: str, asset_class: AssetClass, feed_override: str | None = None
    ) -> RawQuote:
        self._check_open()
        quote = self.quote_at(symbol, asset_class, self._clock())
        return RawQuote(symbol=quote.symbol, bid=quote.bid, ask=quote.ask, timestamp=quote.timestamp,
                        source=self._source(asset_class, feed_override))

    async def get_latest_option_quotes(self, symbols: list[str]) -> dict[str, RawQuote]:
        self._check_open()
        now = self._clock()
        return {symbol.upper(): self.quote_at(symbol.upper(), AssetClass.OPTION, now) for symbol in symbols}

    async def get_options_chain(
        self, underlying_symbol: str, expiration_gte: str, expiration_lte: str
    ) -> list[AlpacaOptionContract]:
        """Active, tradable contracts expiring in [gte, lte], as AlpacaClient
        returns them -- minus any that have already expired by the clock."""
        self._check_open()
        today = self._session.trade_date(self._clock()).isoformat()
        return [
            contract for contract in self._chains.get(underlying_symbol.upper(), ())
            if contract.status == "active" and contract.tradable
            and expiration_gte <= contract.expiration_date <= expiration_lte and contract.expiration_date >= today
        ]

    def _bar_visible_from(self, bar: RawBar, asset_class: AssetClass) -> datetime:
        day = date.fromisoformat(bar.date)
        if asset_class != AssetClass.CRYPTO and not self._session.always:
            close = self._session.close_on(day)
            if close is None:  # set_bars admits only session days
                raise SimulatorScriptError(f"bar {bar.date} has no session")
            return close
        return datetime.combine(day + timedelta(days=1), time(0), NEW_YORK).astimezone(UTC)

    async def get_bars(
        self, symbol: str, asset_class: AssetClass, start: datetime, end: datetime, limit: int = 250
    ) -> list[RawBar]:
        """The most recent ``limit`` completed daily bars stamped within
        [start, end], oldest first, as AlpacaClient returns them."""
        self._check_open()
        now = self._clock()
        start, end = _aware_utc(start, "start"), _aware_utc(end, "end")
        visible = []
        for bar in self._bars.get((_quote_symbol(symbol), asset_class), ()):
            stamp = datetime.combine(date.fromisoformat(bar.date), time(0), NEW_YORK).astimezone(UTC)
            if start <= stamp <= end and self._bar_visible_from(bar, asset_class) <= now:
                visible.append(bar)
        return visible[-limit:] if limit > 0 else []

    # --- BrokerPort: orders and activities (execution arrives in Rev.134) ------

    async def place_order(self, order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        self._check_open()
        raise NotImplementedError("SimulatedBroker order execution arrives in Rev.134")

    async def get_order(self, broker_order_id: str) -> AlpacaOrderResponse:
        self._check_open()
        raise AlpacaError("order not found", 404, None, None, "getOrder")

    async def get_order_by_client_order_id(self, client_order_id: str) -> AlpacaOrderResponse | None:
        self._check_open()
        return None

    async def get_open_orders(self) -> list[AlpacaOrderResponse]:
        self._check_open()
        return []

    async def cancel_order(self, broker_order_id: str) -> None:
        self._check_open()
        raise AlpacaError("order not found", 404, None, None, "cancelOrder")

    async def get_activities(
        self, activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100,
        *, after_id: str | None = None, page_evidence: list[Any] | None = None,
    ) -> list[AlpacaActivity]:
        """No order can have executed yet, so the population is empty. The
        arguments are validated and page evidence recorded as AlpacaClient
        does for its terminal page."""
        self._check_open()
        if not 1 <= page_size <= 100:
            raise ValueError("invalid activity page size")
        if page_evidence is not None:
            from tradepulse.persistence.codec import encode_payload

            params: dict[str, str] = {"page_size": str(page_size), "direction": "asc"}
            if activity_type is not None:
                params["activity_types"] = activity_type
            if since is not None:
                params["after"] = since.isoformat()
            if after_id:
                params["page_token"] = after_id
            page: list[Any] = []
            page_evidence.append({"request": params, "activity_ids": [], "received_at": self._clock().isoformat(),
                                  "response_hash": sha256(encode_payload(page).encode()).hexdigest(),
                                  "terminal": True})
        return []
