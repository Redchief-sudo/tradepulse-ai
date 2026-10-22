"""SimulatedBroker -- an in-memory BrokerPort that models Alpaca's contract.

Phase A0, Rev.133: the foundation. Time comes only from a ManualClock, the
trading calendar from a SimSession (a real exchange calendar or an explicit
one), and market data from scripted time series that are always read as of a
point in time, never ahead of it. The account is cash plus marked positions,
replayed from a timestamped history so equity at any past instant (and so
``last_equity``) is computed, never stored by a call.

Rev.134 adds execution. ``place_order`` computes a deterministic event
schedule (start, fills, scripted terminal, day expiry); fee postings join the
same timeline as fills apply. Every read first materializes the events due at
``clock()`` -- in time order, each exactly once -- and then only observes, so
how often or from where the simulator is polled never changes what happens.
Once a read has observed time t, scripting a quote at or before t is refused:
observed history is immutable. IDs come from a seeded generator, so identical
scripts produce identical orders and activities.

Alpaca behaviour this follows, with sources (checked 2026-10-22):

- order statuses (docs.alpaca.markets/docs/orders-at-alpaca): an order routed
  in session starts ``new``; one received outside the session is ``accepted``
  and queued, then routed (``new``) at the next open; ``filled``, ``canceled``,
  ``expired`` and ``rejected`` are terminal;
- a day order received after the close is queued for the next trading day
  (same page); an unfilled day order ends at the close. The docs call that
  "automatically canceled" while a user report shows ``expired``: the status
  is the ``day_order_end_status`` flag, default ``"expired"``;
- cancelling a filled, canceled or expired order is a 422
  (docs.alpaca.markets/reference/deleteorderforaccount); Alpaca staff confirmed
  that paper answers 204 instead (forum.alpaca.markets/t/5676): flag
  ``paper_cancel_terminal_returns_204``, default False;
- POST /v2/orders answers 403 for insufficient buying power or shares and 422
  for unprocessable input (docs.alpaca.markets/reference/postorder); a
  duplicate client_order_id is not documented, so its status is the
  ``duplicate_client_order_id_status`` flag, default 422;
- activities (docs.alpaca.markets/reference/getaccountactivities): with
  ``direction=asc`` a page begins immediately after ``page_token``, the ID of
  the last activity of the previous page; ``after`` filters on creation time.
  A FILL has no ``created_at``, so it filters on ``transaction_time``. A token
  that is not an existing ID is treated as a lexicographic bound [scenario;
  checked read-only on paper in Task 8];
- FILL and FEE identity, raw keys and timing follow preserved soak evidence:
  FILL IDs carry New York time; FEE and CFEE IDs carry the date at midnight,
  so a late fee sorts before that day's fills; equity REG/TAF/CAT batches post
  after the close, option REG/TAF/ORF batches post the same afternoon, an OCC
  fee follows each option fill (its ``execution_id`` is the fill's ID suffix),
  and CFEE is charged in the coin on crypto buys and in USD on crypto sells.

Not modelled (refused, never approximated): stop, stop-limit and trailing
orders; time in force other than ``day`` and ``gtc``; extended hours; replace;
buying-power checks on buys. A sell larger than the position less open sells
is a 403.

Other assumptions that are not verified Alpaca behaviour are named here:

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
  ``last_equity`` is the starting equity;
- [scenario] an unscripted order fills completely, ``fill_latency`` after it
  starts, at the ask (buy) or bid (sell) of the quote at that instant; a limit
  order fills only when that quote is marketable, otherwise it waits for the
  next quote;
- [scenario] an option without a scripted contract has multiplier 100;
- [scenario] fee rates, rounding and posting times are explicit FeeScenario
  inputs (no defaults); a CFEE row's ``cusip`` is the coin followed by
  ``123456`` and its ``price`` is the fill price to the cent (one soak sample).

It can never be built from Settings or build_broker, and refuses construction
when TRADEPULSE_EXECUTION_MODE is live.
"""

from __future__ import annotations

import bisect
import decimal
import heapq
import os
import random
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from tradepulse.models import AssetClass, Side
from tradepulse.models.market import canonical_venue

from .alpaca_client import paginate_activities, parse_order_data
from .errors import AlpacaError
from .symbols import infer_alpaca_asset_class, normalize_alpaca_symbol
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
_OCC_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
OPEN_STATUSES = frozenset({"new", "accepted", "partially_filled"})
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
_ROUNDING_MODES = frozenset({
    decimal.ROUND_UP, decimal.ROUND_DOWN, decimal.ROUND_CEILING, decimal.ROUND_FLOOR,
    decimal.ROUND_HALF_UP, decimal.ROUND_HALF_DOWN, decimal.ROUND_HALF_EVEN, decimal.ROUND_05UP,
})
_CENT = Decimal("0.01")


class SimulatorScriptError(RuntimeError):
    """The scenario script is missing something a real broker would always
    have (for example a price for a held position). Raised instead of
    inventing the missing value."""


class SimulatorInvariantError(RuntimeError):
    """An order or activity invariant failed. Always a simulator bug."""


@dataclass(frozen=True)
class FeeRates:
    """[scenario] Every rate is an explicit input; nothing defaults.

    Amounts are rounded to the cent with ``rounding`` (a ``decimal`` mode;
    the soak evidence fits ROUND_UP)."""

    reg_per_dollar: Decimal  # equity and option sells, on proceeds
    taf_per_share: Decimal  # equity sells
    taf_max_per_trade: Decimal
    opt_taf_per_contract: Decimal  # option sells
    cat_per_share: Decimal  # equity buys and sells
    orf_per_contract: Decimal  # option sells
    occ_per_contract: Decimal  # every option fill
    crypto_rate: Decimal  # CFEE, on quantity (buys) or notional (sells)
    rounding: str

    def __post_init__(self) -> None:
        for name in ("reg_per_dollar", "taf_per_share", "taf_max_per_trade", "opt_taf_per_contract",
                     "cat_per_share", "orf_per_contract", "occ_per_contract", "crypto_rate"):
            _decimal(getattr(self, name), name, minimum=Decimal(0))
        if self.rounding not in _ROUNDING_MODES:
            raise ValueError(f"unknown rounding mode {self.rounding!r}")

    def cents(self, amount: Decimal) -> Decimal:
        return amount.quantize(_CENT, rounding=self.rounding)


@dataclass(frozen=True)
class FeePosting:
    """[scenario] When fees post, from the soak evidence's shape. Batch times
    are New York wall-clock times on the trade date."""

    equity_batch_ny: time  # REG, TAF, CAT (observed 20:05-20:52 ET)
    option_batch_ny: time  # OPT REG, OPT TAF, ORF (observed 16:31 ET)
    occ_delay: timedelta  # after each option fill (observed ~11 s)
    cfee_asset_delay: timedelta  # after a crypto buy (observed 2-5 s)
    cfee_usd_delay: timedelta  # after a crypto sell (observed ~4 h 17 min)


@dataclass(frozen=True)
class FeeScenario:
    rates: FeeRates | None
    posting: FeePosting | None

    @classmethod
    def none(cls) -> FeeScenario:
        return cls(None, None)

    @classmethod
    def observed_alpaca_2026(cls, rates: FeeRates, posting: FeePosting) -> FeeScenario:
        return cls(rates, posting)


def _fmt(value: Decimal) -> str:
    """Alpaca's number strings: no exponent, no trailing zeros ("1.3", "0")."""
    text = format(value.normalize(), "f")
    return "0" if value == 0 else text


def _ts(at: datetime) -> str:
    """Alpaca's timestamp strings: UTC with six fractional digits and Z."""
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%f}Z"


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


@dataclass(frozen=True, slots=True)
class _ScriptedFill:
    offset: timedelta
    qty: Decimal
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class _Event:
    kind: Literal["start", "fill", "terminal", "expire", "fee_batch", "fee_row"]
    order_id: str = ""
    qty: Decimal | None = None  # fill: None = everything still open
    price: Decimal | None = None  # fill: None = the as-of quote
    status: str = ""  # terminal
    batch: tuple[str, str, date] | None = None  # fee_batch: (sub-type, product, trade date)
    row: tuple[tuple[str, Any], ...] = ()  # fee_row: the raw row less its id
    cash_delta: Decimal = Decimal(0)  # fee_row
    coin_delta: tuple[str, Decimal] | None = None  # fee_row: CFEE in the coin


@dataclass
class _Order:
    id: str
    client_order_id: str
    symbol: str
    asset_class: AssetClass
    side: Side
    qty: Decimal
    order_type: str
    time_in_force: str
    limit_price: Decimal | None
    multiplier: Decimal
    created_at: datetime
    seq: int
    status: str
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    filled_qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    fill_events: int = 0
    checked_filled_qty: Decimal = Decimal(0)
    terminal_snapshot: dict[str, Any] | None = None

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled_qty

    def raw(self) -> dict[str, Any]:
        """The Order schema of POST/GET /v2/orders (docs.alpaca.markets/reference/postorder)."""
        def when(at: datetime | None) -> str | None:
            return _ts(at) if at is not None else None
        return {
            "id": self.id, "client_order_id": self.client_order_id, "created_at": when(self.created_at),
            "updated_at": when(self.updated_at), "submitted_at": when(self.submitted_at),
            "filled_at": when(self.filled_at), "expired_at": when(self.expired_at),
            "canceled_at": when(self.canceled_at), "failed_at": None, "replaced_at": None, "replaced_by": None,
            "replaces": None, "asset_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"tradepulse-sim-asset:{self.symbol}")),
            "symbol": self.symbol, "asset_class": _ALPACA_ASSET_CLASS[self.asset_class], "notional": None,
            "qty": _fmt(self.qty), "filled_qty": _fmt(self.filled_qty),
            "filled_avg_price": _fmt(self.filled_avg_price) if self.filled_avg_price is not None else None,
            "order_class": "", "order_type": self.order_type, "type": self.order_type, "side": self.side.value,
            "position_intent": None, "time_in_force": self.time_in_force,
            "limit_price": _fmt(self.limit_price) if self.limit_price is not None else None, "stop_price": None,
            "status": self.status, "extended_hours": False, "legs": None, "trail_percent": None,
            "trail_price": None, "hwm": None, "expires_at": None, "ratio_qty": None,
        }


@dataclass
class _FeeBatch:
    sub_type: str
    product: str  # "equity" | "option"
    trade_date: date
    created_at: datetime
    amount: Decimal = Decimal(0)  # unrounded
    proceeds: Decimal = Decimal(0)
    units: Decimal = Decimal(0)  # shares or contracts
    trades: int = 0
    posted: bool = False


@dataclass(frozen=True, slots=True)
class _Activity:
    id: str
    visible_at: datetime
    sort_time: datetime  # what ``after`` filters: transaction_time (FILL) or created_at
    raw: dict[str, Any] = field(hash=False)


class SimulatedBroker:
    """An in-memory BrokerPort. See the module docstring for what it models."""

    def __init__(
        self, *, clock: ManualClock, session: SimSession, venue: str = "alpaca", implementation: str = "simulated",
        cash: Decimal = Decimal("100000"), account_number: str = "SIM0001",
        fee_scenario: FeeScenario | None = None, seed: int = 0,
        day_order_end_status: Literal["expired", "canceled"] = "expired",
        paper_cancel_terminal_returns_204: bool = False, duplicate_client_order_id_status: int = 422,
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
        self._fees = fee_scenario or FeeScenario.none()
        self._rng = random.Random(seed)
        self._day_order_end_status = day_order_end_status
        self._paper_cancel_terminal_returns_204 = paper_cancel_terminal_returns_204
        self._duplicate_status = duplicate_client_order_id_status
        self._fill_latency = timedelta(0)
        self._activity_lag = timedelta(0)
        self._events: list[tuple[datetime, int, _Event]] = []
        self._seq = 0
        self._materialized_at: datetime | None = None
        self._orders: dict[str, _Order] = {}
        self._by_client_id: dict[str, str] = {}
        self._scripted: dict[str, tuple[list[_ScriptedFill], str]] = {}
        self._parked: dict[tuple[str, AssetClass], list[tuple[datetime, _Event]]] = {}
        self._activities: list[_Activity] = []
        self._batches: dict[tuple[str, str, date], _FeeBatch] = {}

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
        if self._materialized_at is not None and quote.at < self._materialized_at:
            raise ValueError(f"a read already observed {self._materialized_at}; observed history is immutable")
        key = (_quote_symbol(symbol), asset_class)
        series = self._quotes.setdefault(key, [])
        if series and quote.at <= series[-1].at:
            raise ValueError(f"quote times must strictly increase: {quote.at} after {series[-1].at}")
        series.append(quote)
        for waited_since, event in self._parked.pop(key, []):  # limit orders waiting for a new quote
            self._push(max(quote.at, waited_since), event)

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
        self._materialize(self._clock())
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
        self._materialize(now)
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
        self._materialize(now)
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

    # --- execution scripting -----------------------------------------------------

    def set_fill_latency(self, latency: timedelta) -> None:
        """[scenario] How long after its start an unscripted order fills."""
        if latency < timedelta(0):
            raise ValueError("fill latency cannot be negative")
        self._fill_latency = latency

    def set_activity_lag(self, lag: timedelta) -> None:
        """[scenario] How long after its fill a FILL activity becomes visible."""
        if lag < timedelta(0):
            raise ValueError("activity lag cannot be negative")
        self._activity_lag = lag

    def schedule_fills(
        self, client_order_id: str, fills: Sequence[tuple[timedelta, Decimal, Decimal | None]],
        terminal: Literal["filled", "canceled", "expired"] = "filled",
    ) -> None:
        """Script the execution of the order that will be placed with this
        client_order_id. Offsets are from the order's start (acceptance, or
        the next open if it is queued). A None price takes the as-of quote. A
        ``canceled`` or ``expired`` terminal follows the last fill."""
        if client_order_id in self._by_client_id or client_order_id in self._scripted:
            raise ValueError(f"{client_order_id} is already placed or scripted")
        if terminal not in ("filled", "canceled", "expired"):
            raise ValueError(f"unknown terminal {terminal!r}")
        scripted = []
        previous = timedelta(0)
        for offset, qty, price in fills:
            if offset < previous:
                raise ValueError("fill offsets must not decrease")
            previous = offset
            _decimal(qty, "fill qty", positive=True)
            if price is not None:
                _decimal(price, "fill price", positive=True)
            scripted.append(_ScriptedFill(offset, qty, price))
        if terminal == "filled" and not scripted:
            raise ValueError("a filled order needs at least one fill")
        self._scripted[client_order_id] = (scripted, terminal)

    # --- the event timeline -------------------------------------------------------

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def _push(self, at: datetime, event: _Event) -> None:
        self._seq += 1
        heapq.heappush(self._events, (at, self._seq, event))

    def _materialize(self, now: datetime) -> None:
        """Apply, in time order, every event due at ``now`` that has not been
        applied. Idempotent and monotonic: the clock never goes back."""
        while self._events and self._events[0][0] <= now:
            at, _, event = heapq.heappop(self._events)
            self._apply(at, event)
        if self._materialized_at is None or now > self._materialized_at:
            self._materialized_at = now
        self._check_invariants()

    def _apply(self, at: datetime, event: _Event) -> None:
        if event.kind in ("fee_batch", "fee_row"):
            self._post_fee(at, event)
            return
        order = self._orders[event.order_id]
        if order.status in TERMINAL_STATUSES:
            return  # nothing changes a terminal order; later events lapse
        if event.kind == "start":
            order.status, order.submitted_at, order.updated_at = "new", at, at
        elif event.kind == "terminal":
            self._finish(order, event.status, at)
        elif event.kind == "expire":
            self._finish(order, self._day_order_end_status, at)
        else:
            self._fill(order, at, event)

    def _finish(self, order: _Order, status: str, at: datetime) -> None:
        order.status, order.updated_at = status, at
        if status == "canceled":
            order.canceled_at = at
        elif status == "expired":
            order.expired_at = at

    def _fill(self, order: _Order, at: datetime, event: _Event) -> None:
        quote = self.quote_at(order.symbol, order.asset_class, at)
        touch = quote.ask if order.side == Side.BUY else quote.bid
        price = event.price if event.price is not None else touch
        if order.order_type == "limit":
            assert order.limit_price is not None
            marketable = touch is not None and (
                touch <= order.limit_price if order.side == Side.BUY else touch >= order.limit_price)
            if not marketable:
                self._rearm(order, at, event)
                return
            if price is not None and (price > order.limit_price if order.side == Side.BUY
                                      else price < order.limit_price):
                raise SimulatorScriptError(f"scripted fill {price} is worse than limit {order.limit_price}")
        if price is None or price <= 0:
            raise SimulatorScriptError(f"{order.symbol} has no usable quote at {at.isoformat()} to fill against")
        qty = event.qty if event.qty is not None else order.remaining
        if qty <= 0 or qty > order.remaining:
            raise SimulatorScriptError(f"fill of {qty} exceeds the {order.remaining} still open on {order.id}")

        order.filled_qty += qty
        order.notional += qty * price
        order.filled_avg_price = order.notional / order.filled_qty
        order.status = "filled" if order.filled_qty == order.qty else "partially_filled"
        order.updated_at = at
        if order.status == "filled":
            order.filled_at = at
        order.fill_events += 1

        t_ny = at.astimezone(NEW_YORK)
        suffix = self._uuid()
        activity_id = f"{t_ny:%Y%m%d%H%M%S}{t_ny.microsecond // 1000:03d}::{suffix}"
        raw = {
            "activity_type": "FILL", "cum_qty": _fmt(order.filled_qty), "id": activity_id,
            "leaves_qty": _fmt(order.remaining), "order_id": order.id, "order_status": order.status,
            "price": _fmt(price), "qty": _fmt(qty), "side": order.side.value, "symbol": order.symbol,
            "transaction_time": _ts(at), "type": "fill" if order.status == "filled" else "partial_fill",
        }
        self._activities.append(_Activity(activity_id, at + self._activity_lag, at, raw))
        self._book_fill(order, at, qty, price)
        self._accrue_fees(order, at, qty, price, suffix)

    def _rearm(self, order: _Order, at: datetime, event: _Event) -> None:
        """A limit fill that is not marketable waits for the next quote."""
        key = (order.symbol, order.asset_class)
        later = [quote.at for quote in self._quotes.get(key, []) if quote.at > at]
        if later:
            self._push(later[0], event)
        else:
            self._parked.setdefault(key, []).append((at, event))

    def _book_fill(self, order: _Order, at: datetime, qty: Decimal, price: Decimal) -> None:
        state = self._state_at(at)
        holdings = dict(state.holdings)
        held = holdings.get(order.symbol)
        cash = state.cash
        if order.side == Side.BUY:
            cash -= qty * price * order.multiplier
            if held is None:
                holdings[order.symbol] = _Holding(order.asset_class, qty, price, order.multiplier)
            else:
                new_qty = held.qty + qty
                average = (held.qty * held.avg_entry_price + qty * price) / new_qty
                holdings[order.symbol] = _Holding(held.asset_class, new_qty, average, held.multiplier)
        else:
            if held is None or held.qty < qty:
                raise SimulatorScriptError(f"sell of {qty} {order.symbol} exceeds the position at {at.isoformat()}")
            cash += qty * price * order.multiplier
            if held.qty == qty:
                del holdings[order.symbol]
            else:
                holdings[order.symbol] = _Holding(held.asset_class, held.qty - qty, held.avg_entry_price,
                                                  held.multiplier)
        self._record(_State(at, cash, tuple(sorted(holdings.items(), key=lambda item: item[0]))))

    # --- fees -----------------------------------------------------------------------

    def _accrue_fees(self, order: _Order, at: datetime, qty: Decimal, price: Decimal, fill_suffix: str) -> None:
        rates, posting = self._fees.rates, self._fees.posting
        if rates is None or posting is None:
            return
        sell = order.side == Side.SELL
        trade_date = SimSession.trade_date(at)
        if order.asset_class == AssetClass.EQUITY:
            self._batch(at, "CAT", "equity", trade_date, posting.equity_batch_ny,
                        amount=qty * rates.cat_per_share, units=qty)
            if sell:
                self._batch(at, "REG", "equity", trade_date, posting.equity_batch_ny,
                            amount=qty * price * rates.reg_per_dollar, proceeds=qty * price)
                self._batch(at, "TAF", "equity", trade_date, posting.equity_batch_ny,
                            amount=min(qty * rates.taf_per_share, rates.taf_max_per_trade), units=qty)
        elif order.asset_class == AssetClass.OPTION:
            created = at + posting.occ_delay
            amount = rates.cents(qty * rates.occ_per_contract)
            if amount > 0:
                self._push(created, _Event("fee_row", row=(
                    ("activity_sub_type", "OCC"), ("activity_type", "FEE"), ("created_at", _ts(created)),
                    ("currency", "USD"), ("date", SimSession.trade_date(created).isoformat()),
                    ("description", "OCC Clearing Fee"), ("execution_id", fill_suffix),
                    ("net_amount", _fmt(-amount)), ("status", "executed"),
                ), cash_delta=-amount))
            if sell:
                proceeds = qty * price * order.multiplier
                self._batch(at, "REG", "option", trade_date, posting.option_batch_ny,
                            amount=proceeds * rates.reg_per_dollar, proceeds=proceeds)
                self._batch(at, "TAF", "option", trade_date, posting.option_batch_ny,
                            amount=qty * rates.opt_taf_per_contract, units=qty)
                self._batch(at, "ORF", "option", trade_date, posting.option_batch_ny,
                            amount=qty * rates.orf_per_contract, units=qty)
        else:
            coin = order.symbol.split("/")[0]
            if not sell:
                created = at + posting.cfee_asset_delay
                fee_qty = qty * rates.crypto_rate
                if fee_qty > 0:
                    self._push(created, _Event("fee_row", row=(
                        ("activity_type", "CFEE"), ("created_at", _ts(created)), ("currency", "USD"),
                        ("cusip", f"{coin}123456"), ("date", SimSession.trade_date(created).isoformat()),
                        ("description", "Coin Pair Transaction Fee (Non USD)"), ("net_amount", "0"),
                        ("price", _fmt(price.quantize(_CENT, rounding=decimal.ROUND_HALF_EVEN))),
                        ("qty", _fmt(-fee_qty)), ("status", "executed"), ("symbol", order.symbol.replace("/", "")),
                    ), coin_delta=(order.symbol, -fee_qty)))
            else:
                created = at + posting.cfee_usd_delay
                amount = rates.cents(qty * price * rates.crypto_rate)
                if amount > 0:
                    self._push(created, _Event("fee_row", row=(
                        ("activity_type", "CFEE"), ("created_at", _ts(created)), ("currency", "USD"),
                        ("date", SimSession.trade_date(created).isoformat()),
                        ("description", "Coin Pair Transaction Fee (USD)"), ("net_amount", _fmt(-amount)),
                        ("status", "executed"),
                    ), cash_delta=-amount))

    def _batch(
        self, at: datetime, sub_type: str, product: str, trade_date: date, posting_ny: time, *,
        amount: Decimal, proceeds: Decimal = Decimal(0), units: Decimal = Decimal(0),
    ) -> None:
        """Accrue a fill into its (sub-type, product, trade date) batch. The
        batch is one posting event, created when its first fill applies."""
        key = (sub_type, product, trade_date)
        batch = self._batches.get(key)
        if batch is None:
            created = datetime.combine(trade_date, posting_ny, NEW_YORK).astimezone(UTC)
            batch = self._batches[key] = _FeeBatch(sub_type, product, trade_date, created)
            self._push(created, _Event("fee_batch", batch=key))
        if batch.posted or at >= batch.created_at:
            raise SimulatorScriptError(f"a {trade_date} fill at {at.isoformat()} is after its {sub_type} batch posted")
        batch.amount += amount
        batch.proceeds += proceeds
        batch.units += units
        batch.trades += 1

    def _post_fee(self, at: datetime, event: _Event) -> None:
        rates = self._fees.rates
        assert rates is not None
        if event.kind == "fee_row":
            raw = dict(event.row)
            raw["id"] = f"{date.fromisoformat(raw['date']):%Y%m%d}000000000::{self._uuid()}"
            self._activities.append(_Activity(raw["id"], at, at, dict(sorted(raw.items()))))
            self._book_fee(at, event.cash_delta, event.coin_delta)
            return
        assert event.batch is not None
        batch = self._batches[event.batch]
        batch.posted = True
        amount = rates.cents(batch.amount)
        if amount <= 0:
            return
        day = batch.trade_date.isoformat()
        account = self._account_number
        prefix = "OPT " if batch.product == "option" else ""
        units = _fmt(batch.units)
        description = {
            "REG": f"{prefix}REG fee for proceed of ${_fmt(rates.cents(batch.proceeds))} on {day} by {account}",
            "TAF": (f"{prefix}TAF fee for proceed of {units} {'contracts' if prefix else 'shares'}"
                    f" ({batch.trades} trades) on {day} by {account}"),
            "CAT": f"CAT fee for proceed of {batch.trades} trades on {day} by {account}",
            "ORF": f"ORF fee for proceed of {units} contracts on {day} by {account}",
        }[batch.sub_type]
        activity_id = f"{batch.trade_date:%Y%m%d}000000000::{self._uuid()}"
        raw = {
            "activity_sub_type": batch.sub_type, "activity_type": "FEE", "created_at": _ts(at), "currency": "USD",
            "date": day, "description": description, "id": activity_id, "net_amount": _fmt(-amount),
            "status": "executed",
        }
        self._activities.append(_Activity(activity_id, at, at, raw))
        self._book_fee(at, -amount, None)

    def _book_fee(self, at: datetime, cash_delta: Decimal, coin_delta: tuple[str, Decimal] | None) -> None:
        state = self._state_at(at)
        holdings = dict(state.holdings)
        if coin_delta is not None:
            symbol, delta = coin_delta
            held = holdings.get(symbol)
            if held is None or held.qty + delta < 0:
                raise SimulatorScriptError(f"a {symbol} fee of {delta} exceeds the position at {at.isoformat()}")
            if held.qty + delta == 0:
                del holdings[symbol]
            else:
                holdings[symbol] = _Holding(held.asset_class, held.qty + delta, held.avg_entry_price,
                                            held.multiplier)
        self._record(_State(at, state.cash + cash_delta, tuple(sorted(holdings.items(), key=lambda item: item[0]))))

    # --- invariants -------------------------------------------------------------------

    def _check_invariants(self) -> None:
        fills_by_order = Counter(a.raw["order_id"] for a in self._activities if a.raw["activity_type"] == "FILL")
        for order in self._orders.values():
            problem = None
            if not 0 <= order.filled_qty <= order.qty:
                problem = f"filled_qty {order.filled_qty} outside [0, {order.qty}]"
            elif order.filled_qty < order.checked_filled_qty:
                problem = f"filled_qty went down from {order.checked_filled_qty} to {order.filled_qty}"
            elif order.filled_qty and order.filled_avg_price != order.notional / order.filled_qty:
                problem = f"filled_avg_price {order.filled_avg_price} is not the VWAP"
            elif not order.filled_qty and order.filled_avg_price is not None:
                problem = "an unfilled order has an average price"
            elif fills_by_order[order.id] != order.fill_events:
                problem = f"{fills_by_order[order.id]} FILL activities for {order.fill_events} fills"
            elif (order.status == "filled") != (order.filled_qty == order.qty):
                problem = f"status {order.status} with {order.filled_qty} of {order.qty} filled"
            elif order.terminal_snapshot is not None and order.raw() != order.terminal_snapshot:
                problem = "a terminal order changed"
            if problem:
                raise SimulatorInvariantError(f"order {order.id}: {problem}")
            order.checked_filled_qty = order.filled_qty
            if order.status in TERMINAL_STATUSES and order.terminal_snapshot is None:
                order.terminal_snapshot = order.raw()

    # --- BrokerPort: orders -------------------------------------------------------------

    def _classify(self, symbol: str) -> tuple[str, AssetClass]:
        raw = symbol.strip().upper()
        if _OCC_SYMBOL.match(raw):
            return raw, AssetClass.OPTION
        asset_class = infer_alpaca_asset_class(raw)
        return normalize_alpaca_symbol(raw, asset_class), asset_class

    def _multiplier(self, symbol: str, asset_class: AssetClass) -> Decimal:
        if asset_class != AssetClass.OPTION:
            return Decimal(1)
        for chain in self._chains.values():
            for contract in chain:
                if contract.occ_symbol.upper() == symbol:
                    return contract.multiplier
        return Decimal(100)

    async def place_order(self, order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        self._check_open()
        now = self._clock()
        self._materialize(now)
        symbol, asset_class = self._classify(order.symbol)
        if order.order_type not in ("market", "limit"):
            raise SimulatorScriptError(f"order type {order.order_type!r} is not modelled")
        if order.time_in_force not in ("day", "gtc"):
            raise SimulatorScriptError(f"time in force {order.time_in_force!r} is not modelled")
        if not isinstance(order.qty, Decimal) or not order.qty.is_finite() or order.qty <= 0:
            raise AlpacaError("qty must be > 0", 422, None, None, "placeOrder")
        if order.order_type == "limit" and (order.limit_price is None or order.limit_price <= 0):
            raise AlpacaError("limit_price is required for limit orders", 422, None, None, "placeOrder")
        client_order_id = order.client_order_id or ""
        if len(client_order_id) > 128:
            raise AlpacaError("client_order_id must be no more than 128 characters", 422, None, None, "placeOrder")
        if client_order_id in self._by_client_id:
            raise AlpacaError("client_order_id must be unique", self._duplicate_status, None, None, "placeOrder")
        if order.side == Side.SELL:
            held = dict(self._state_at(now).holdings).get(symbol)
            committed = sum((o.remaining for o in self._orders.values()
                             if o.symbol == symbol and o.side == Side.SELL and o.status in OPEN_STATUSES), Decimal(0))
            available = (held.qty if held is not None else Decimal(0)) - committed
            if order.qty > available:
                raise AlpacaError(
                    f"insufficient qty available for order (requested: {_fmt(order.qty)}, available: {_fmt(available)})",
                    403, None, None, "placeOrder")

        open_now = asset_class == AssetClass.CRYPTO or self._session.is_open(now)
        start = now if open_now else self._session.next_open(now)
        if start is None:
            raise SimulatorScriptError(f"no session after {now.isoformat()} in the configured calendar")
        # Every check runs before any state changes: a refused submission
        # leaves no order, no client-ID mapping, no events and no ID draws.
        # A refused script is discarded, so a corrected one can be scheduled
        # under the same client_order_id.
        scripted, terminal = self._scripted.get(client_order_id, ([], "filled"))
        total = sum((fill.qty for fill in scripted), Decimal(0))
        if scripted and (total > order.qty or (terminal == "filled" and total != order.qty)):
            del self._scripted[client_order_id]
            raise SimulatorScriptError(f"scripted fills total {total} for an order of {order.qty}")
        self._scripted.pop(client_order_id, None)

        self._seq += 1
        client_order_id = client_order_id or self._uuid()  # Alpaca generates one when none is sent
        placed = _Order(
            id=self._uuid(), client_order_id=client_order_id, symbol=symbol, asset_class=asset_class,
            side=order.side, qty=order.qty, order_type=order.order_type, time_in_force=order.time_in_force,
            limit_price=order.limit_price if order.order_type == "limit" else None,
            multiplier=self._multiplier(symbol, asset_class), created_at=now, seq=self._seq,
            status="new" if open_now else "accepted", submitted_at=now if open_now else None, updated_at=now,
        )
        self._orders[placed.id] = placed
        self._by_client_id[client_order_id] = placed.id

        if not open_now:
            self._push(start, _Event("start", placed.id))
        if scripted:
            for fill in scripted:
                self._push(start + fill.offset, _Event("fill", placed.id, qty=fill.qty, price=fill.price))
            if terminal != "filled":
                self._push(start + scripted[-1].offset, _Event("terminal", placed.id, status=terminal))
        elif terminal == "filled":
            self._push(start + self._fill_latency, _Event("fill", placed.id))
        if order.time_in_force == "day" and asset_class != AssetClass.CRYPTO:
            close = self._session.next_close(start)
            if close is not None:
                self._push(close, _Event("expire", placed.id))
        return parse_order_data(placed.raw(), request_id=None)

    def _order_by_id(self, broker_order_id: str, operation: str) -> _Order:
        found = self._orders.get(broker_order_id)
        if found is None:
            raise AlpacaError("order not found", 404, None, None, operation)
        return found

    async def get_order(self, broker_order_id: str) -> AlpacaOrderResponse:
        self._check_open()
        self._materialize(self._clock())
        return parse_order_data(self._order_by_id(broker_order_id, "getOrder").raw(), request_id=None)

    async def get_order_by_client_order_id(self, client_order_id: str) -> AlpacaOrderResponse | None:
        self._check_open()
        self._materialize(self._clock())
        order_id = self._by_client_id.get(client_order_id)
        return None if order_id is None else parse_order_data(self._orders[order_id].raw(), request_id=None)

    async def get_open_orders(self) -> list[AlpacaOrderResponse]:
        self._check_open()
        self._materialize(self._clock())
        open_orders = sorted((o for o in self._orders.values() if o.status in OPEN_STATUSES),
                             key=lambda o: (o.created_at, o.seq), reverse=True)
        return [parse_order_data(o.raw(), request_id=None) for o in open_orders]

    async def cancel_order(self, broker_order_id: str) -> None:
        """Effective at clock(). A terminal order is a 422, or silently
        accepted with ``paper_cancel_terminal_returns_204``."""
        self._check_open()
        now = self._clock()
        self._materialize(now)
        order = self._order_by_id(broker_order_id, "cancelOrder")
        if order.status in TERMINAL_STATUSES:
            if self._paper_cancel_terminal_returns_204:
                return
            raise AlpacaError(f"order is already in \"{order.status}\" state", 422, None, None, "cancelOrder")
        self._finish(order, "canceled", now)
        self._check_invariants()

    # --- BrokerPort: activities -------------------------------------------------------

    async def get_activities(
        self, activity_type: str | None = "FILL", since: datetime | None = None, page_size: int = 100,
        *, after_id: str | None = None, page_evidence: list[Any] | None = None,
    ) -> list[AlpacaActivity]:
        """The visible activity population in ascending ID order, paged by the
        same loop AlpacaClient uses (``paginate_activities``)."""
        self._check_open()
        now = self._clock()
        self._materialize(now)

        async def fetch_page(params: dict[str, str]) -> Any:
            types = set(params["activity_types"].split(",")) if "activity_types" in params else None
            after = datetime.fromisoformat(params["after"]) if "after" in params else None
            token = params.get("page_token")
            rows = sorted(
                (a for a in self._activities
                 if a.visible_at <= now and (types is None or a.raw["activity_type"] in types)
                 and (after is None or a.sort_time > after) and (token is None or a.id > token)),
                key=lambda a: a.id,
            )
            return [dict(a.raw) for a in rows[: int(params["page_size"])]]

        return await paginate_activities(
            fetch_page, self._clock, activity_type, since, page_size, after_id=after_id, page_evidence=page_evidence,
        )
