"""SimulatedBroker foundation (Phase A0 Task 4, Rev.133): manual clock,
exchange calendar, as-of market data and account arithmetic."""
from __future__ import annotations

import asyncio
import decimal
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from lease_support import held_test_lease

from tradepulse.alerts import TelegramAlerter
from tradepulse.broker import AlpacaError, is_definitive_rejection
from tradepulse.broker.port import BrokerPort, account_ref
from tradepulse.broker.simulated import (
    Fault,
    FeePosting,
    FeeRates,
    FeeScenario,
    ManualClock,
    SimSession,
    SimulatedBroker,
    SimulatorInvariantError,
    SimulatorScriptError,
)
from tradepulse.broker.types import AlpacaOptionContract, AlpacaOrderRequest, RawBar
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    SessionState,
    Side,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.risk import save_session
from tradepulse.settlement import SettlementProcessor

T0 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Monday, 10:00 New York, market open


def _session() -> SimSession:
    return SimSession.from_exchange_calendar("XNYS", date(2026, 10, 1), date(2026, 12, 31))


def _broker(start: datetime = T0, **kwargs: object) -> SimulatedBroker:
    return SimulatedBroker(clock=ManualClock(start), session=_session(), **kwargs)  # type: ignore[arg-type]


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_simulator_satisfies_the_port_with_alpaca_venue_and_simulated_provenance() -> None:
    broker = _broker()
    assert isinstance(broker, BrokerPort)
    assert broker.venue == "alpaca"
    assert broker.implementation == "simulated"


def test_venue_is_canonicalized_and_unknown_venues_refused() -> None:
    assert _broker(venue="ALPACA").venue == "alpaca"
    with pytest.raises(ValueError):
        _broker(venue="nasdaq")


# --- calendar -------------------------------------------------------------


def test_xnys_calendar_handles_dst_holidays_and_early_closes() -> None:
    session = _session()
    # 2026-10-30 is EDT (UTC-4); 2026-11-02 is EST (UTC-5) after the 11-01 switch.
    assert session.next_open(_utc(2026, 10, 30, 0, 0)) == _utc(2026, 10, 30, 13, 30)
    assert session.next_open(_utc(2026, 11, 1, 0, 0)) == _utc(2026, 11, 2, 14, 30)
    # Thanksgiving is closed; the day after closes early at 13:00 New York.
    assert session.close_on(date(2026, 11, 26)) is None
    assert session.next_open(_utc(2026, 11, 25, 22, 0)) == _utc(2026, 11, 27, 14, 30)
    assert session.close_on(date(2026, 11, 27)) == _utc(2026, 11, 27, 18, 0)
    assert session.is_open(_utc(2026, 11, 27, 17, 59))
    assert not session.is_open(_utc(2026, 11, 27, 18, 0))


def test_session_open_is_inclusive_and_close_exclusive() -> None:
    session = _session()
    assert not session.is_open(_utc(2026, 10, 19, 13, 29, 59))
    assert session.is_open(_utc(2026, 10, 19, 13, 30))
    assert not session.is_open(_utc(2026, 10, 19, 20, 0))
    # While open, next_open is the following session and next_close today's close.
    assert session.next_open(T0) == _utc(2026, 10, 20, 13, 30)
    assert session.next_close(T0) == _utc(2026, 10, 19, 20, 0)


def test_trade_date_is_new_york_date() -> None:
    assert _session().trade_date(_utc(2026, 10, 16, 1, 30)) == date(2026, 10, 15)


def test_explicit_sessions_are_validated() -> None:
    a = (_utc(2026, 1, 2, 14, 30), _utc(2026, 1, 2, 21, 0))
    b = (_utc(2026, 1, 5, 14, 30), _utc(2026, 1, 5, 21, 0))
    assert SimSession.explicit([a, b]).sessions == (a, b)
    with pytest.raises(ValueError):
        SimSession.explicit([b, a])  # out of order
    with pytest.raises(ValueError):
        SimSession.explicit([(a[1], a[0])])  # close before open
    with pytest.raises(ValueError):
        SimSession.explicit([(a[0], _utc(2026, 1, 5, 15, 0)), b])  # overlap
    with pytest.raises(ValueError):
        SimSession.explicit([(datetime(2026, 1, 2, 14, 30), a[1])])  # noqa: DTZ001 -- naive on purpose


def test_always_open_session() -> None:
    session = SimSession.always_open()
    assert session.is_open(_utc(2026, 11, 26, 3, 0))
    assert session.next_open(T0) is None
    assert session.next_close(T0) is None


def test_queries_past_the_calendar_end_answer_none_not_a_guess() -> None:
    session = SimSession.from_exchange_calendar("XNYS", date(2026, 10, 1), date(2026, 10, 31))
    assert session.next_open(_utc(2026, 10, 30, 21, 0)) is None


# --- clock ----------------------------------------------------------------


def test_manual_clock_never_goes_backwards() -> None:
    clock = ManualClock(T0)
    clock.advance(timedelta(minutes=5))
    assert clock() == T0 + timedelta(minutes=5)
    clock.set(T0 + timedelta(hours=1))
    assert clock() == T0 + timedelta(hours=1)
    with pytest.raises(ValueError):
        clock.set(T0)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 10, 19, 14, 0))  # noqa: DTZ001 -- naive on purpose


async def test_get_clock_reflects_the_session_at_the_manual_time() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    state = await broker.get_clock()
    assert (state.is_open, state.next_close, state.timestamp) == (True, _utc(2026, 10, 19, 20, 0), T0)
    clock.set(_utc(2026, 10, 19, 21, 0))
    state = await broker.get_clock()
    assert (state.is_open, state.next_open) == (False, _utc(2026, 10, 20, 13, 30))


# --- market data ----------------------------------------------------------


async def test_quote_lookup_is_as_of_the_requested_time() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("100"), Decimal("100.10"), at=T0 - timedelta(minutes=1))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("101"), Decimal("101.10"), at=T0 + timedelta(minutes=1))
    quote = await broker.get_latest_quote("aapl", AssetClass.EQUITY)
    assert (quote.symbol, quote.bid, quote.ask, quote.timestamp) == ("AAPL", Decimal("100"), Decimal("100.10"),
                                                                     T0 - timedelta(minutes=1))
    assert quote.source == "alpaca_iex"
    clock.advance(timedelta(minutes=1))
    assert (await broker.get_latest_quote("AAPL", AssetClass.EQUITY)).bid == Decimal("101")
    assert broker.quote_at("AAPL", AssetClass.EQUITY, T0).bid == Decimal("100")


async def test_quote_series_must_be_strictly_increasing_and_decimal() -> None:
    broker = _broker()
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("2"), at=T0)
    with pytest.raises(ValueError):
        broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("2"), at=T0)
    with pytest.raises(ValueError):
        broker.set_quote("MSFT", AssetClass.EQUITY, 1.0, Decimal("2"), at=T0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        broker.set_quote("MSFT", AssetClass.EQUITY, Decimal("-1"), Decimal("2"), at=T0)


async def test_unscripted_quote_is_empty_not_invented() -> None:
    broker = _broker()
    quote = await broker.get_latest_quote("MSFT", AssetClass.EQUITY)
    assert (quote.symbol, quote.bid, quote.ask, quote.timestamp) == ("MSFT", None, None, None)
    # A quote scripted only for the future is not visible yet either.
    broker.set_quote("MSFT", AssetClass.EQUITY, Decimal("1"), Decimal("2"), at=T0 + timedelta(seconds=1))
    assert (await broker.get_latest_quote("MSFT", AssetClass.EQUITY)).bid is None


async def test_quote_sources_follow_the_configured_feeds() -> None:
    broker = _broker()
    occ = "AAPL261120C00200000"
    broker.set_quote("BTC/USD", AssetClass.CRYPTO, Decimal("1"), Decimal("2"), at=T0)
    broker.set_quote(occ, AssetClass.OPTION, Decimal("3"), Decimal("4"), at=T0)
    assert (await broker.get_latest_quote("BTC-USD", AssetClass.CRYPTO)).source == "alpaca_crypto"
    assert (await broker.get_latest_quote("BTC-USD", AssetClass.CRYPTO)).bid == Decimal("1")
    assert (await broker.get_latest_quote("AAPL", AssetClass.EQUITY, feed_override="sip")).source == "alpaca_sip"
    broker.set_market_data_feeds(equity_feed="sip", option_feed="opra")
    assert (await broker.get_latest_quote("AAPL", AssetClass.EQUITY)).source == "alpaca_sip"
    quotes = await broker.get_latest_option_quotes([occ.lower(), "AAPL261120P00200000"])
    assert quotes[occ].ask == Decimal("4") and quotes[occ].source == "alpaca_opra"
    assert quotes["AAPL261120P00200000"].bid is None


def _bar(day: str, close: str) -> RawBar:
    c = Decimal(close)
    return RawBar(date=day, open=c, high=c, low=c, close=c, volume=Decimal("1000"))


async def test_bars_and_option_chain_are_filtered_by_date_and_expiry() -> None:
    clock = ManualClock(_utc(2026, 10, 21, 15, 0))  # Wednesday, session open
    broker = SimulatedBroker(clock=clock, session=_session())
    broker.set_bars("AAPL", AssetClass.EQUITY, [_bar("2026-10-16", "1"), _bar("2026-10-19", "2"),
                                                 _bar("2026-10-20", "3"), _bar("2026-10-21", "4")])
    window = (_utc(2026, 10, 17), _utc(2026, 10, 31))
    # Today's bar is not visible until today's session has closed.
    assert [b.date for b in await broker.get_bars("AAPL", AssetClass.EQUITY, *window)] == ["2026-10-19", "2026-10-20"]
    assert [b.date for b in await broker.get_bars("AAPL", AssetClass.EQUITY, *window, limit=1)] == ["2026-10-20"]
    clock.set(_utc(2026, 10, 21, 20, 0))
    assert [b.close for b in await broker.get_bars("AAPL", AssetClass.EQUITY, *window)] == [
        Decimal("2"), Decimal("3"), Decimal("4")]

    def contract(occ: str, expiry: str, *, status: str = "active", tradable: bool = True) -> AlpacaOptionContract:
        return AlpacaOptionContract(occ_symbol=occ, underlying_symbol="AAPL", option_type="call",
                                    strike_price=Decimal("200"), expiration_date=expiry, multiplier=Decimal("100"),
                                    status=status, tradable=tradable)

    broker.set_option_chain("AAPL", [
        contract("AAPL261016C00200000", "2026-10-16"),  # already expired
        contract("AAPL261023C00200000", "2026-10-23"),
        contract("AAPL261030C00200000", "2026-10-30", tradable=False),
        contract("AAPL261106C00200000", "2026-11-06", status="inactive"),
        contract("AAPL261120C00200000", "2026-11-20"),
        contract("AAPL261218C00200000", "2026-12-18"),
    ])
    chain = await broker.get_options_chain("aapl", "2026-10-01", "2026-11-30")
    assert [c.occ_symbol for c in chain] == ["AAPL261023C00200000", "AAPL261120C00200000"]


def test_bars_and_contracts_are_validated_when_scripted() -> None:
    broker = _broker()
    with pytest.raises(ValueError):
        broker.set_bars("AAPL", AssetClass.EQUITY, [_bar("2026-10-19", "2"), _bar("2026-10-16", "1")])
    with pytest.raises(ValueError):
        broker.set_bars("AAPL", AssetClass.EQUITY, [_bar("2026-10-19", "0")])
    other = AlpacaOptionContract(occ_symbol="MSFT261120C00200000", underlying_symbol="MSFT", option_type="call",
                                 strike_price=Decimal("200"), expiration_date="2026-11-20",
                                 multiplier=Decimal("100"), status="active", tradable=True)
    with pytest.raises(ValueError):
        broker.set_option_chain("AAPL", [other])


# --- account --------------------------------------------------------------


async def test_account_equity_is_cash_plus_marked_positions() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), cash=Decimal("10000"))
    occ = "AAPL261120C00200000"
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("10"), Decimal("100"))
    broker.seed_position(occ, AssetClass.OPTION, Decimal("2"), Decimal("1.50"), multiplier=Decimal("100"))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("109"), Decimal("111"), at=T0)
    broker.set_quote(occ, AssetClass.OPTION, Decimal("2.00"), Decimal("2.20"), at=T0)
    account = await broker.get_account()
    # 10 x 110 + 2 x 2.10 x 100 = 1100 + 420
    assert account.equity == Decimal("11520")
    assert account.cash == Decimal("10000")
    assert account.portfolio_value == account.equity
    assert account.equity_components == {"long_market_value": Decimal("1520"), "short_market_value": Decimal("0")}
    assert account.equity == account.cash + account.equity_components["long_market_value"]
    assert account.account_number == "SIM0001"
    assert (await account_ref(broker)).key == "alpaca:SIM0001"

    positions = {p.symbol: p for p in await broker.get_positions()}
    option = positions[occ]
    assert (option.qty, option.current_price, option.market_value, option.cost_basis, option.unrealized_pl) == (
        Decimal("2"), Decimal("2.10"), Decimal("420.00"), Decimal("300.00"), Decimal("120.00"))
    assert option.raw["asset_class"] == "us_option"
    assert broker.last_positions_received_at == T0


async def test_last_equity_is_computed_at_the_most_recent_close() -> None:
    clock = ManualClock(_utc(2026, 10, 19, 14, 0))
    broker = SimulatedBroker(clock=clock, session=_session(), cash=Decimal("1000"))
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("10"), Decimal("100"))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("100"), Decimal("100"), at=_utc(2026, 10, 19, 14, 0))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("120"), Decimal("120"), at=_utc(2026, 10, 19, 19, 59))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("130"), Decimal("130"), at=_utc(2026, 10, 20, 14, 0))
    # Before any close since the simulator started: the starting state.
    assert (await broker.get_account()).last_equity == Decimal("2000")
    clock.set(_utc(2026, 10, 20, 15, 0))
    account = await broker.get_account()
    assert account.equity == Decimal("2300")
    assert account.last_equity == Decimal("2200")  # marked at Monday's 20:00 UTC close


async def test_a_held_position_without_any_quote_fails_loudly() -> None:
    broker = _broker()
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("100"))
    with pytest.raises(SimulatorScriptError):
        await broker.get_account()


def test_seeded_positions_are_validated() -> None:
    broker = _broker()
    with pytest.raises(ValueError):
        broker.seed_position("AAPL261120C00200000", AssetClass.OPTION, Decimal("1"), Decimal("1"))  # no multiplier
    with pytest.raises(ValueError):
        broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("0"), Decimal("1"))
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("1"))
    with pytest.raises(ValueError):
        broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("1"))  # already held


# --- empty book ---------------------------------------------------------------


async def test_an_empty_book_reports_no_orders_or_activities() -> None:
    broker = _broker()
    assert await broker.get_open_orders() == []
    assert await broker.get_order_by_client_order_id("x") is None
    with pytest.raises(AlpacaError) as missing:
        await broker.get_order("nope")
    assert missing.value.status_code == 404
    with pytest.raises(AlpacaError):
        await broker.cancel_order("nope")
    evidence: list[object] = []
    assert await broker.get_activities(page_evidence=evidence) == []
    assert len(evidence) == 1 and evidence[0]["terminal"] is True  # type: ignore[index]
    with pytest.raises(ValueError):
        await broker.get_activities(page_size=101)


# --- safety ---------------------------------------------------------------


def test_live_mode_refuses_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADEPULSE_EXECUTION_MODE", " Live ")
    with pytest.raises(RuntimeError):
        _broker()


async def test_closed_simulator_refuses_calls() -> None:
    broker = _broker()
    await broker.aclose()
    with pytest.raises(RuntimeError):
        await broker.get_account()
    assert broker.rate_limit_snapshot is None


def test_crossed_quotes_are_rejected_unless_the_scenario_allows_them() -> None:
    broker = _broker()
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("100"), Decimal("100"), at=T0)  # locked is legal
    with pytest.raises(ValueError):
        broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("100.01"), Decimal("100"), at=T0 + timedelta(seconds=1))
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("100.01"), Decimal("100"), at=T0 + timedelta(seconds=1),
                     allow_crossed=True)
    assert broker.quote_at("AAPL", AssetClass.EQUITY, T0 + timedelta(seconds=1)).bid == Decimal("100.01")


async def test_exchange_bars_must_fall_on_calendar_sessions() -> None:
    broker = _broker()
    for day in ("2026-10-18",   # Sunday
                "2026-11-26",   # Thanksgiving
                "2026-09-30"):  # before the configured calendar starts
        with pytest.raises(ValueError):
            broker.set_bars("AAPL", AssetClass.EQUITY, [_bar(day, "1")])
        with pytest.raises(ValueError):
            broker.set_bars("AAPL261120C00200000", AssetClass.OPTION, [_bar(day, "1")])
    broker.set_bars("BTC/USD", AssetClass.CRYPTO, [_bar("2026-10-18", "1")])  # crypto trades every day
    always = SimulatedBroker(clock=ManualClock(_utc(2026, 10, 20)), session=SimSession.always_open())
    always.set_bars("AAPL", AssetClass.EQUITY, [_bar("2026-10-18", "1")])
    assert [b.date for b in await always.get_bars("AAPL", AssetClass.EQUITY, _utc(2026, 10, 1), _utc(2026, 10, 31))] == [
        "2026-10-18"]


# === Rev.134: scheduled execution, activities, fees ===========================

RATES = FeeRates(
    reg_per_dollar=Decimal("0.0000206"), taf_per_share=Decimal("0.000166"), taf_max_per_trade=Decimal("8.30"),
    opt_taf_per_contract=Decimal("0.00279"), cat_per_share=Decimal("0.000035"), orf_per_contract=Decimal("0.02295"),
    occ_per_contract=Decimal("0.025"), crypto_rate=Decimal("0.0025"), rounding=decimal.ROUND_UP,
)
POSTING = FeePosting(equity_batch_ny=time(20, 16), option_batch_ny=time(16, 31), occ_delay=timedelta(seconds=12),
                     cfee_asset_delay=timedelta(seconds=3), cfee_usd_delay=timedelta(hours=4, minutes=17))
FEES = FeeScenario.observed_alpaca_2026(RATES, POSTING)
OCC = "AAPL261120C00200000"


def _order(symbol: str, qty: str, side: Side = Side.BUY, coid: str | None = None, **kwargs: object) -> AlpacaOrderRequest:
    return AlpacaOrderRequest(symbol=symbol, qty=Decimal(qty), side=side, client_order_id=coid, **kwargs)  # type: ignore[arg-type]


def _quote(broker: SimulatedBroker, symbol: str, bid: str, ask: str, at: datetime,
           asset_class: AssetClass = AssetClass.EQUITY) -> None:
    broker.set_quote(symbol, asset_class, Decimal(bid), Decimal(ask), at=at)


async def _fills(broker: SimulatedBroker) -> list[dict[str, object]]:
    return [dict(a.raw) for a in await broker.get_activities(activity_type=None)]


# --- scheduling ----------------------------------------------------------------


async def test_market_buy_in_session_fills_at_the_ask_with_one_fill_activity() -> None:
    broker = _broker(cash=Decimal("10000"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "10", coid="c1"))
    assert (placed.status, placed.filled_qty) == ("new", Decimal("0"))
    order = await broker.get_order(placed.broker_order_id)
    assert (order.status, order.filled_qty, order.filled_avg_price) == ("filled", Decimal("10"), Decimal("100.10"))
    assert order.raw["client_order_id"] == "c1" and order.raw["time_in_force"] == "day"
    assert order.raw["type"] == "market" and order.raw["qty"] == "10"
    (fill,) = await broker.get_activities()
    assert set(fill.raw) == {"activity_type", "cum_qty", "id", "leaves_qty", "order_id", "order_status", "price",
                             "qty", "side", "symbol", "transaction_time", "type"}
    assert (fill.raw["price"], fill.raw["type"], fill.raw["order_id"], fill.qty) == (
        "100.1", "fill", placed.broker_order_id, Decimal("10"))
    (position,) = await broker.get_positions()
    assert (position.symbol, position.qty, position.avg_entry_price) == ("AAPL", Decimal("10"), Decimal("100.10"))
    assert (await broker.get_account()).cash == Decimal("8999.00")


async def _scenario(polls: int) -> tuple[object, ...]:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), seed=7, fee_scenario=FEES)
    _quote(broker, "AAPL", "100", "100.10", T0)
    _quote(broker, "AAPL", "101", "101.10", T0 + timedelta(seconds=6))
    broker.schedule_fills("c1", [(timedelta(seconds=5), Decimal("3"), None), (timedelta(seconds=8), Decimal("2"), None)])
    placed = await broker.place_order(_order("AAPL", "5", coid="c1"))

    async def poller() -> None:
        for _ in range(polls // 2):
            await broker.get_order(placed.broker_order_id)
            await broker.get_activities()
            await asyncio.sleep(0)

    if polls:
        for step in range(50):
            clock.set(T0 + timedelta(seconds=10) * (step + 1) / 50)
            await asyncio.gather(poller(), poller())
    clock.set(T0 + timedelta(seconds=10))
    order = await broker.get_order(placed.broker_order_id)
    return (order.raw, await _fills(broker), [p.raw for p in await broker.get_positions()])


async def test_polling_frequency_never_changes_execution() -> None:
    assert await _scenario(0) == await _scenario(50)


async def test_reads_never_advance_scheduled_fills() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.schedule_fills("c1", [(timedelta(seconds=5), Decimal("2"), None), (timedelta(seconds=10), Decimal("3"), None)])
    placed = await broker.place_order(_order("AAPL", "5", coid="c1"))
    clock.set(T0 + timedelta(seconds=7))
    for _ in range(20):
        order = await broker.get_order(placed.broker_order_id)
        assert (order.status, order.filled_qty) == ("partially_filled", Decimal("2"))
    assert len(await broker.get_activities()) == 1


# --- sessions --------------------------------------------------------------------


async def test_market_order_out_of_session_queues_until_the_open_then_fills_at_the_open_quote() -> None:
    clock = ManualClock(_utc(2026, 10, 19, 21, 0))  # Monday, after the close
    broker = SimulatedBroker(clock=clock, session=_session())
    _quote(broker, "AAPL", "100", "101", _utc(2026, 10, 19, 20, 30))
    _quote(broker, "AAPL", "110", "111", _utc(2026, 10, 20, 13, 30))
    placed = await broker.place_order(_order("AAPL", "1"))
    assert placed.status == "accepted" and placed.submitted_at is None
    clock.set(_utc(2026, 10, 20, 13, 29, 59))
    assert (await broker.get_order(placed.broker_order_id)).status == "accepted"
    assert await broker.get_activities() == []
    clock.set(_utc(2026, 10, 20, 13, 30))
    order = await broker.get_order(placed.broker_order_id)
    assert (order.status, order.filled_avg_price, order.submitted_at) == ("filled", Decimal("111"),
                                                                         _utc(2026, 10, 20, 13, 30))


async def test_day_order_open_at_close_expires_keeping_partial_fill() -> None:
    clock = ManualClock(_utc(2026, 10, 19, 19, 0))
    broker = SimulatedBroker(clock=clock, session=_session())
    _quote(broker, "AAPL", "100", "100.10", _utc(2026, 10, 19, 19, 0))
    broker.schedule_fills("c1", [(timedelta(minutes=1), Decimal("2"), None), (timedelta(hours=7), Decimal("3"), None)])
    placed = await broker.place_order(_order("AAPL", "5", coid="c1"))
    clock.set(_utc(2026, 10, 20, 15, 0))
    order = await broker.get_order(placed.broker_order_id)
    assert (order.status, order.filled_qty) == ("expired", Decimal("2"))
    assert order.raw["expired_at"] == "2026-10-19T20:00:00.000000Z"
    assert len(await broker.get_activities()) == 1
    assert await broker.get_open_orders() == []


async def test_day_order_end_status_can_be_canceled() -> None:
    clock = ManualClock(_utc(2026, 10, 19, 19, 59))
    broker = SimulatedBroker(clock=clock, session=_session(), day_order_end_status="canceled")
    broker.schedule_fills("c1", [], terminal="expired")
    placed = await broker.place_order(_order("AAPL", "5", coid="c1"))
    clock.set(_utc(2026, 10, 19, 20, 0))
    assert (await broker.get_order(placed.broker_order_id)).status == "canceled"


# --- partial fills and cancellation ------------------------------------------------


async def test_scheduled_partials_vwap_and_cancel_keep_the_filled_part() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    broker.schedule_fills("c1", [(timedelta(seconds=1), Decimal("3"), Decimal("100"))], terminal="canceled")
    placed = await broker.place_order(_order("AAPL", "5", coid="c1"))
    clock.set(T0 + timedelta(seconds=2))
    order = await broker.get_order(placed.broker_order_id)
    assert (order.status, order.filled_qty, order.filled_avg_price) == ("canceled", Decimal("3"), Decimal("100"))

    broker.schedule_fills("c2", [(timedelta(seconds=1), Decimal("1"), Decimal("100")),
                                 (timedelta(seconds=2), Decimal("2"), Decimal("103"))], terminal="canceled")
    second = await broker.place_order(_order("AAPL", "5", coid="c2"))
    clock.advance(timedelta(seconds=1))
    await broker.cancel_order(second.broker_order_id)  # after the first fill, before the second
    clock.advance(timedelta(seconds=5))
    order = await broker.get_order(second.broker_order_id)
    assert (order.status, order.filled_qty, order.filled_avg_price) == ("canceled", Decimal("1"), Decimal("100"))

    broker.schedule_fills("c3", [(timedelta(seconds=1), Decimal("1"), Decimal("100")),
                                 (timedelta(seconds=2), Decimal("2"), Decimal("103"))])
    third = await broker.place_order(_order("AAPL", "3", coid="c3"))
    clock.advance(timedelta(seconds=2))
    assert (await broker.get_order(third.broker_order_id)).filled_avg_price == Decimal("102")


async def test_cancel_on_a_terminal_order_is_422() -> None:
    for paper_bug in (False, True):
        broker = _broker(paper_cancel_terminal_returns_204=paper_bug)
        _quote(broker, "AAPL", "100", "100.10", T0)
        placed = await broker.place_order(_order("AAPL", "1"))
        assert (await broker.get_order(placed.broker_order_id)).status == "filled"
        if paper_bug:
            await broker.cancel_order(placed.broker_order_id)
        else:
            with pytest.raises(AlpacaError) as rejected:
                await broker.cancel_order(placed.broker_order_id)
            assert rejected.value.status_code == 422
        assert (await broker.get_order(placed.broker_order_id)).status == "filled"


async def test_invariant_violation_raises() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "2"))
    await broker.get_order(placed.broker_order_id)
    broker._orders[placed.broker_order_id].filled_qty = Decimal("3")  # corrupt it
    with pytest.raises(SimulatorInvariantError):
        await broker.get_order(placed.broker_order_id)


# --- activity identity -------------------------------------------------------------


async def test_fill_ids_use_new_york_time() -> None:
    at = datetime(2026, 10, 15, 13, 56, 53, 634768, tzinfo=UTC)
    broker = _broker(start=at)
    _quote(broker, "AAPL", "100", "100.10", at)
    await broker.place_order(_order("AAPL", "1"))
    (fill,) = await broker.get_activities()
    assert fill.activity_id.startswith("20261015095653634::")
    assert fill.raw["transaction_time"] == "2026-10-15T13:56:53.634768Z"
    assert fill.transaction_time == at


async def test_fee_ids_use_trade_date_midnight_and_sort_before_that_days_fills() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES)
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("100"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    await broker.place_order(_order("AAPL", "100", Side.SELL))
    clock.set(_utc(2026, 10, 20, 0, 16))  # 20:16 New York, Monday
    rows = await broker.get_activities(activity_type=None)
    assert [r.activity_type for r in rows] == ["FEE", "FEE", "FEE", "FILL"]
    assert all(r.activity_id.startswith("20261019000000000::") for r in rows[:3])
    assert {r.raw["activity_sub_type"] for r in rows[:3]} == {"REG", "TAF", "CAT"}
    for row in rows[:3]:
        assert set(row.raw) == {"activity_sub_type", "activity_type", "created_at", "currency", "date",
                                "description", "id", "net_amount", "status"}
        assert (row.raw["date"], row.raw["created_at"]) == ("2026-10-19", "2026-10-20T00:16:00.000000Z")
    reg = next(r.raw for r in rows if r.raw.get("activity_sub_type") == "REG")
    # 100 x 100 = $10000 proceeds x 0.0000206 = 0.206, rounded up to the cent
    assert reg["net_amount"] == "-0.21"
    assert reg["description"] == "REG fee for proceed of $10000 on 2026-10-19 by SIM0001"
    taf = next(r.raw for r in rows if r.raw.get("activity_sub_type") == "TAF")
    assert taf["description"] == "TAF fee for proceed of 100 shares (1 trades) on 2026-10-19 by SIM0001"


# --- ordering and pagination --------------------------------------------------------


async def _same_millisecond_fills(broker: SimulatedBroker, clock: ManualClock) -> None:
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.schedule_fills("c1", [(timedelta(seconds=1), Decimal("1"), None)] * 3)
    broker.schedule_fills("c2", [(timedelta(seconds=2), Decimal("1"), None)])
    await broker.place_order(_order("AAPL", "3", coid="c1"))
    await broker.place_order(_order("AAPL", "1", coid="c2"))
    clock.set(T0 + timedelta(seconds=3))


async def test_population_is_ascending_id_including_same_millisecond_fills() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    await _same_millisecond_fills(broker, clock)
    ids = [a.activity_id for a in await broker.get_activities()]
    assert len(ids) == 4 and ids == sorted(ids)
    assert len({i.split("::")[0] for i in ids[:3]}) == 1


async def test_pagination_boundary_inside_a_millisecond_is_complete() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    await _same_millisecond_fills(broker, clock)
    evidence: list[dict[str, object]] = []
    paged = [a.activity_id for a in await broker.get_activities(page_size=2, page_evidence=evidence)]
    whole = [a.activity_id for a in await broker.get_activities()]
    assert paged == whole and len(set(paged)) == 4
    pages = [e["activity_ids"] for e in evidence]
    assert [len(p) for p in pages] == [2, 2, 0]  # type: ignore[arg-type]
    assert pages[0][-1].split("::")[0] == pages[1][0].split("::")[0]  # type: ignore[index]
    assert [e["terminal"] for e in evidence] == [False, False, True]


async def test_late_fee_sorts_before_an_existing_cursor() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES)
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("10"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    await broker.place_order(_order("AAPL", "10", Side.SELL))
    clock.set(_utc(2026, 10, 19, 19, 0))
    cursor = (await broker.get_activities(activity_type=None))[-1].activity_id
    clock.set(_utc(2026, 10, 20, 1, 0))
    after_cursor = await broker.get_activities(activity_type=None, after_id=cursor)
    history = await broker.get_activities(activity_type=None)
    assert after_cursor == []
    assert [a.activity_type for a in history] == ["FEE", "FEE", "FEE", "FILL"]
    assert all(a.activity_id < cursor for a in history[:3])


async def test_after_id_is_a_lexicographic_bound() -> None:
    """As checked read-only on the paper account (2026-10-23): a well-formed
    token bounds lexicographically even when it is no activity's ID; a
    malformed one is a definitive 422."""
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    await _same_millisecond_fills(broker, clock)
    ids = [a.activity_id for a in await broker.get_activities()]
    prefix = ids[0].split("::")[0]
    after_the_millisecond = prefix + "::ffffffff-ffff-4fff-bfff-ffffffffffff"
    before_everything = "20261019000000000::00000000-0000-0000-0000-000000000000"
    assert [a.activity_id for a in await broker.get_activities(after_id=after_the_millisecond)] == ids[3:]
    assert [a.activity_id for a in await broker.get_activities(after_id=before_everything)] == ids
    for malformed in (prefix + "::~", "20261019", ids[0] + " "):
        with pytest.raises(AlpacaError) as rejected:
            await broker.get_activities(after_id=malformed)
        assert (rejected.value.status_code, rejected.value.message) == (422, "invalid page_token")
        assert is_definitive_rejection(rejected.value)


async def test_since_filters_fill_time_and_fee_creation_time() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES)
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("10"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    await broker.place_order(_order("AAPL", "10", Side.SELL))
    clock.set(_utc(2026, 10, 20, 1, 0))
    later = await broker.get_activities(activity_type=None, since=T0)
    assert [a.activity_type for a in later] == ["FEE", "FEE", "FEE"]  # the fill is at T0, not after it


# --- visibility and quantities -------------------------------------------------------


async def test_activity_lag_hides_fills_until_visible_at() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    broker.set_activity_lag(timedelta(seconds=30))
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "1"))
    clock.set(T0 + timedelta(seconds=10))
    assert (await broker.get_order(placed.broker_order_id)).status == "filled"
    assert await broker.get_activities() == []
    clock.set(T0 + timedelta(seconds=30))
    assert len(await broker.get_activities()) == 1


async def test_fractional_and_crypto_quantities_round_trip_exactly() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    _quote(broker, "BTC/USD", "60000", "60010", T0, AssetClass.CRYPTO)
    await broker.place_order(_order("AAPL", "9.877"))
    await broker.place_order(_order("BTC/USD", "0.12345678", time_in_force="gtc"))
    assert {a.symbol: (a.qty, a.raw["qty"]) for a in await broker.get_activities()} == {
        "AAPL": (Decimal("9.877"), "9.877"), "BTC/USD": (Decimal("0.12345678"), "0.12345678")}
    assert {p.symbol: p.qty for p in await broker.get_positions()} == {
        "AAPL": Decimal("9.877"), "BTC/USD": Decimal("0.12345678")}


# --- fees -------------------------------------------------------------------------------


async def test_fee_batch_is_created_once_and_rereads_are_identical() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES)
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("10"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    await broker.place_order(_order("AAPL", "4", Side.SELL))
    clock.set(_utc(2026, 10, 19, 15, 0))
    await broker.place_order(_order("AAPL", "6", Side.SELL))
    clock.set(_utc(2026, 10, 20, 0, 16))
    first = await _fills(broker)
    clock.advance(timedelta(days=2))
    assert await _fills(broker) == first == await _fills(broker)
    fees = [r for r in first if r["activity_type"] == "FEE"]
    assert len(fees) == 3
    taf = next(r for r in fees if r["activity_sub_type"] == "TAF")
    assert taf["description"] == "TAF fee for proceed of 10 shares (2 trades) on 2026-10-19 by SIM0001"
    cash = (await broker.get_account()).cash
    assert cash == Decimal("1000") + Decimal("100000") + sum(Decimal(str(r["net_amount"])) for r in fees)


async def test_fee_batch_aggregates_per_trade_date_in_new_york() -> None:
    # An explicit extended session lets a sell land at 19:59 New York time.
    session = SimSession.explicit([(_utc(2026, 10, 15, 13, 30), _utc(2026, 10, 16, 0, 0)),
                                   (_utc(2026, 10, 16, 13, 30), _utc(2026, 10, 17, 0, 0))])
    clock = ManualClock(_utc(2026, 10, 15, 23, 59))  # 19:59 New York on 10-15
    broker = SimulatedBroker(clock=clock, session=session, fee_scenario=FEES)
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("2"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", clock())
    await broker.place_order(_order("AAPL", "1", Side.SELL))
    clock.set(_utc(2026, 10, 16, 14, 0))
    await broker.place_order(_order("AAPL", "1", Side.SELL))
    clock.set(_utc(2026, 10, 17, 1, 0))
    reg = sorted((r for r in await _fills(broker) if r.get("activity_sub_type") == "REG"), key=lambda r: str(r["id"]))
    assert [(r["date"], r["created_at"]) for r in reg] == [
        ("2026-10-15", "2026-10-16T00:16:00.000000Z"), ("2026-10-16", "2026-10-17T00:16:00.000000Z")]


async def test_option_fees_follow_the_observed_shapes() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES, cash=Decimal("1000"))
    _quote(broker, OCC, "3.00", "3.04", T0, AssetClass.OPTION)
    await broker.place_order(_order(OCC, "1"))
    clock.set(T0 + timedelta(hours=1))
    _quote(broker, OCC, "1.44", "1.50", clock(), AssetClass.OPTION)
    await broker.place_order(_order(OCC, "1", Side.SELL))
    clock.set(_utc(2026, 10, 19, 20, 31))  # 16:31 New York
    rows = await _fills(broker)
    fills = [r for r in rows if r["activity_type"] == "FILL"]
    occ = [r for r in rows if r.get("activity_sub_type") == "OCC"]
    assert len(occ) == 2
    assert {r["execution_id"] for r in occ} == {str(f["id"]).split("::")[1] for f in fills}
    assert {r["net_amount"] for r in occ} == {"-0.03"}
    assert occ[0]["created_at"] == "2026-10-19T14:00:12.000000Z" or occ[1]["created_at"] == "2026-10-19T14:00:12.000000Z"
    descriptions = sorted(str(r["description"]) for r in rows if r["activity_type"] == "FEE"
                          and r["activity_sub_type"] != "OCC")
    assert descriptions == [
        "OPT REG fee for proceed of $144 on 2026-10-19 by SIM0001",
        "OPT TAF fee for proceed of 1 contracts (1 trades) on 2026-10-19 by SIM0001",
        "ORF fee for proceed of 1 contracts on 2026-10-19 by SIM0001",
    ]
    account = await broker.get_account()
    # 1000 - 304 + 144 - fees (OCC 0.03 x 2 + OPT REG 0.01 + OPT TAF 0.01 + ORF 0.03 = 0.11)
    assert account.cash == Decimal("1000") - Decimal("304") + Decimal("144") - Decimal("0.11")


async def test_crypto_fees_are_in_the_coin_on_buys_and_in_usd_on_sells() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), fee_scenario=FEES)
    _quote(broker, "SOL/USD", "105.30", "105.353", T0, AssetClass.CRYPTO)
    await broker.place_order(_order("SOL/USD", "13.9083", time_in_force="gtc"))
    clock.set(T0 + timedelta(seconds=3))
    (cfee,) = [r for r in await _fills(broker) if r["activity_type"] == "CFEE"]
    assert set(cfee) == {"activity_type", "created_at", "currency", "cusip", "date", "description", "id",
                         "net_amount", "price", "qty", "status", "symbol"}
    assert (cfee["qty"], cfee["price"], cfee["net_amount"], cfee["symbol"]) == ("-0.03477075", "105.35", "0", "SOLUSD")
    (position,) = await broker.get_positions()
    assert position.qty == Decimal("13.9083") - Decimal("0.03477075")

    clock.set(T0 + timedelta(minutes=1))
    await broker.place_order(_order("SOL/USD", str(position.qty), Side.SELL, time_in_force="gtc"))
    clock.set(T0 + timedelta(minutes=1, hours=4, seconds=1) + timedelta(minutes=17))
    usd = [r for r in await _fills(broker) if r.get("description") == "Coin Pair Transaction Fee (USD)"]
    assert len(usd) == 1 and set(usd[0]) == {"activity_type", "created_at", "currency", "date", "description",
                                             "id", "net_amount", "status"}
    # 13.87352925 x 105.30 x 0.0025 = 3.6522... rounded up
    assert usd[0]["net_amount"] == "-3.66"
    assert await broker.get_positions() == []


# --- orders -----------------------------------------------------------------------------


async def test_client_order_id_lookup_and_duplicate_is_422() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "1", coid="c1"))
    found = await broker.get_order_by_client_order_id("c1")
    assert found is not None and found.broker_order_id == placed.broker_order_id
    assert await broker.get_order_by_client_order_id("missing") is None
    with pytest.raises(AlpacaError) as duplicate:
        await broker.place_order(_order("AAPL", "1", coid="c1"))
    assert duplicate.value.status_code == 422
    with pytest.raises(AlpacaError) as too_long:
        await broker.place_order(_order("AAPL", "1", coid="x" * 129))
    assert too_long.value.status_code == 422


async def test_account_equity_after_fills_is_cash_plus_marked_positions() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session(), cash=Decimal("10000"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    await broker.place_order(_order("AAPL", "10"))
    clock.set(T0 + timedelta(minutes=1))
    _quote(broker, "AAPL", "104", "106", clock())
    account = await broker.get_account()
    assert account.cash == Decimal("8999.00")
    assert account.equity == Decimal("8999.00") + Decimal("1050")
    assert account.equity == account.cash + account.equity_components["long_market_value"]


async def test_sell_beyond_position_less_open_sells_is_403() -> None:
    clock = ManualClock(_utc(2026, 10, 19, 21, 0))  # closed, so orders stay open
    broker = SimulatedBroker(clock=clock, session=_session())
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("5"), Decimal("90"))
    await broker.place_order(_order("AAPL", "3", Side.SELL))
    with pytest.raises(AlpacaError) as rejected:
        await broker.place_order(_order("AAPL", "3", Side.SELL))
    assert rejected.value.status_code == 403
    assert "available: 2" in str(rejected.value)


async def test_limit_order_waits_for_a_marketable_quote() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    _quote(broker, "AAPL", "99.90", "100", T0)
    placed = await broker.place_order(_order("AAPL", "1", order_type="limit", limit_price=Decimal("99")))
    clock.set(T0 + timedelta(seconds=5))
    assert (await broker.get_order(placed.broker_order_id)).status == "new"
    _quote(broker, "AAPL", "98.40", "98.50", T0 + timedelta(seconds=8))  # scripted after a read, still in the future
    clock.set(T0 + timedelta(seconds=8))
    order = await broker.get_order(placed.broker_order_id)
    assert (order.status, order.filled_avg_price) == ("filled", Decimal("98.50"))
    assert order.raw["limit_price"] == "99"


async def test_observed_history_cannot_be_rescripted() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    await broker.get_open_orders()
    with pytest.raises(ValueError):
        _quote(broker, "AAPL", "1", "2", T0 - timedelta(seconds=1))


async def test_unmodelled_orders_are_refused_not_approximated() -> None:
    broker = _broker()
    with pytest.raises(SimulatorScriptError):
        await broker.place_order(_order("AAPL", "1", order_type="stop", stop_price=Decimal("1")))
    with pytest.raises(SimulatorScriptError):
        await broker.place_order(_order("AAPL", "1", time_in_force="ioc"))


# --- refused submissions leave no trace (audit TP134-01) --------------------------------


def _footprint(broker: SimulatedBroker) -> tuple[object, ...]:
    return (dict(broker._orders), dict(broker._by_client_id), list(broker._events), list(broker._activities),
            list(broker._history), broker._rng.getstate())


@pytest.mark.parametrize(("fills", "terminal", "qty"), [
    ([(timedelta(seconds=1), Decimal("9"), None)], "filled", "2"),  # overfill
    ([(timedelta(seconds=1), Decimal("1"), None)], "filled", "2"),  # a filled terminal that fills less
    ([(timedelta(seconds=1), Decimal("3"), None)], "canceled", "2"),  # overfill before a cancel
])
async def test_an_invalid_script_creates_no_order_and_can_be_corrected(
    fills: list[tuple[timedelta, Decimal, Decimal | None]], terminal: str, qty: str,
) -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.schedule_fills("c1", fills, terminal=terminal)  # type: ignore[arg-type]
    before = _footprint(broker)
    with pytest.raises(SimulatorScriptError):
        await broker.place_order(_order("AAPL", qty, coid="c1"))
    assert _footprint(broker) == before
    assert await broker.get_order_by_client_order_id("c1") is None
    assert await broker.get_open_orders() == []
    assert await broker.get_activities(activity_type=None) == []
    assert (await broker.get_account()).cash == Decimal("100000")

    broker.schedule_fills("c1", [(timedelta(seconds=1), Decimal(qty), None)])  # the corrected script
    placed = await broker.place_order(_order("AAPL", qty, coid="c1"))
    broker._clock.advance(timedelta(seconds=1))
    assert (await broker.get_order(placed.broker_order_id)).status == "filled"

    # The refused attempt consumed no IDs: a simulator that never saw it agrees.
    clean = _broker()
    _quote(clean, "AAPL", "100", "100.10", T0)
    clean.schedule_fills("c1", [(timedelta(seconds=1), Decimal(qty), None)])
    assert (await clean.place_order(_order("AAPL", qty, coid="c1"))).broker_order_id == placed.broker_order_id


async def test_every_refused_submission_leaves_no_trace() -> None:
    broker = _broker()
    broker.seed_position("AAPL", AssetClass.EQUITY, Decimal("1"), Decimal("90"))
    _quote(broker, "AAPL", "100", "100.10", T0)
    before = _footprint(broker)
    refused = [
        _order("AAPL", "0"), _order("AAPL", "1", coid="x" * 129), _order("AAPL", "2", Side.SELL),
        _order("AAPL", "1", order_type="limit"), _order("AAPL", "1", order_type="stop"),
        _order("AAPL", "1", time_in_force="ioc"),
    ]
    for request in refused:
        with pytest.raises((AlpacaError, SimulatorScriptError)):
            await broker.place_order(request)
        assert _footprint(broker) == before


# === Rev.135: fault injection ==================================================


async def _raises(call: object) -> Exception:
    try:
        await call  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 - the test inspects whatever the fault raised
        return exc
    raise AssertionError("no fault fired")


async def test_submit_ambiguous_accepted_records_the_order_then_raises() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    exc = await _raises(broker.place_order(_order("AAPL", "1", coid="c1")))
    assert isinstance(exc, httpx.ConnectError) and not is_definitive_rejection(exc)
    found = await broker.get_order_by_client_order_id("c1")
    assert found is not None and found.status == "filled"
    assert broker.queued_faults == ()


@pytest.mark.parametrize(("fault", "raised", "definitive"), [
    (Fault.SUBMIT_AMBIGUOUS_NOT_ACCEPTED, httpx.ConnectError, False),
    (Fault.SUBMIT_REJECTED, AlpacaError, True),
])
async def test_submit_faults_that_record_nothing(fault: Fault, raised: type[Exception], definitive: bool) -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.inject(fault)
    before = _footprint(broker)
    exc = await _raises(broker.place_order(_order("AAPL", "1", coid="c1")))
    assert isinstance(exc, raised) and is_definitive_rejection(exc) is definitive
    if isinstance(exc, AlpacaError):
        assert exc.status_code == 422
    assert _footprint(broker) == before
    assert await broker.get_order_by_client_order_id("c1") is None
    placed = await broker.place_order(_order("AAPL", "1", coid="c1"))  # the fault fired once
    assert placed.status == "new"


async def test_lookup_timeout_hits_order_lookups_only() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "1", coid="c1"))
    broker.inject(Fault.LOOKUP_TIMEOUT, times=2)
    await broker.get_account()  # out of scope: unaffected, fault still queued
    for call in (broker.get_order(placed.broker_order_id), broker.get_order_by_client_order_id("c1")):
        exc = await _raises(call)
        assert isinstance(exc, httpx.ReadTimeout) and not is_definitive_rejection(exc)
    assert (await broker.get_order(placed.broker_order_id)).status == "filled"


async def test_rate_limited_hits_any_method_as_a_429() -> None:
    broker = _broker()
    broker.inject(Fault.RATE_LIMITED, times=2)
    for call in (broker.get_clock(), broker.get_bars("AAPL", AssetClass.EQUITY, T0, T0)):
        exc = await _raises(call)
        assert isinstance(exc, AlpacaError) and exc.status_code == 429 and not is_definitive_rejection(exc)
    await broker.get_clock()


async def test_disconnected_fails_every_call_until_reconnect_and_keeps_the_queue() -> None:
    clock = ManualClock(T0)
    broker = SimulatedBroker(clock=clock, session=_session())
    _quote(broker, "AAPL", "100", "100.10", T0)
    broker.schedule_fills("c1", [(timedelta(seconds=5), Decimal("1"), None)])
    placed = await broker.place_order(_order("AAPL", "1", coid="c1"))
    broker.inject(Fault.SUBMIT_REJECTED)
    broker.inject(Fault.DISCONNECTED)
    for call in (broker.get_order(placed.broker_order_id), broker.place_order(_order("AAPL", "1")),
                 broker.get_activities(), broker.get_latest_quote("AAPL", AssetClass.EQUITY)):
        exc = await _raises(call)
        assert isinstance(exc, httpx.ConnectError) and not is_definitive_rejection(exc)
    assert broker.queued_faults == ((Fault.SUBMIT_REJECTED, 1),)
    clock.set(T0 + timedelta(seconds=10))
    broker.reconnect()
    # The outage never moved the scheduled fill.
    (fill,) = await broker.get_activities()
    assert fill.transaction_time == T0 + timedelta(seconds=5)


async def test_fault_precedence_is_first_in_scope_and_disconnect_overrides() -> None:
    broker = _broker()
    _quote(broker, "AAPL", "100", "100.10", T0)
    placed = await broker.place_order(_order("AAPL", "1", coid="c1"))
    broker.inject(Fault.LOOKUP_TIMEOUT)
    broker.inject(Fault.SUBMIT_REJECTED)
    broker.inject(Fault.DISCONNECTED)
    assert isinstance(await _raises(broker.place_order(_order("AAPL", "1"))), httpx.ConnectError)
    assert isinstance(await _raises(broker.get_order(placed.broker_order_id)), httpx.ConnectError)
    assert broker.queued_faults == ((Fault.LOOKUP_TIMEOUT, 1), (Fault.SUBMIT_REJECTED, 1))
    broker.reconnect()
    rejected = await _raises(broker.place_order(_order("AAPL", "1")))
    assert isinstance(rejected, AlpacaError) and rejected.status_code == 422
    assert isinstance(await _raises(broker.get_order(placed.broker_order_id)), httpx.ReadTimeout)
    assert broker.queued_faults == ()


def test_fault_injection_is_validated() -> None:
    with pytest.raises(ValueError):
        _broker().inject(Fault.RATE_LIMITED, times=0)


# --- through the real ExecutionGateway ---------------------------------------------

GATEWAY_NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)  # Monday, session open


async def _gateway(tmp_path: object) -> tuple[PersistenceRepositories, SimulatedBroker, ExecutionGateway]:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    # The broker's clock runs a second ahead of the gateway's: the broker
    # processes an order after the gateway timestamps its intent, and the
    # gateway reads fills strictly after that timestamp (Alpaca's ``after``).
    broker = SimulatedBroker(
        clock=ManualClock(GATEWAY_NOW + timedelta(seconds=1)),
        session=SimSession.from_exchange_calendar("XNYS", date(2026, 8, 1), date(2026, 9, 30)),
    )
    broker.set_quote("AAPL", AssetClass.EQUITY, Decimal("199.50"), Decimal("199.60"), at=GATEWAY_NOW)
    alerts = TelegramAlerter(None, None)
    settlement = SettlementProcessor(repositories, alerts, clock=lambda: GATEWAY_NOW)
    gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker), settlement, alerts,
                               risk_limits_for_profile("balanced"), ExecutionMode.PAPER, clock=lambda: GATEWAY_NOW,
                               lease=held_test_lease())
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, GATEWAY_NOW))
    return repositories, broker, gateway


def _buy() -> ExecutionRequest:
    return ExecutionRequest(asset=AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL"), side=Side.BUY,
                            requested_quantity=Decimal("5"), strategy="test", confidence=Decimal("90"),
                            idempotency_key="k1")


async def _intent_status(repositories: PersistenceRepositories) -> TradeIntentStatus:
    [row] = await repositories.trade_intents.list_all()
    return hydrate("trade_intents", row["payload"]).status


async def test_faults_classified_in_operation_context(tmp_path: object) -> None:
    # Accepted but the response was lost: recovery finds the order by client
    # order id, adopts it and settles it. One order only.
    repositories, broker, gateway = await _gateway(tmp_path / "accepted")  # type: ignore[operator]
    broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    result = await gateway.execute_intent(_buy())
    assert (result.status, result.filled_quantity) == ("filled", Decimal("5"))
    assert await _intent_status(repositories) == TradeIntentStatus.FILLED
    assert len(broker._orders) == 1

    # Never accepted: the lookup finds nothing. The current gateway does not
    # conclude absence; it holds the intent as SUBMISSION_UNKNOWN for an
    # operator and never resubmits, even when the same intent is re-executed.
    repositories, broker, gateway = await _gateway(tmp_path / "not-accepted")  # type: ignore[operator]
    broker.inject(Fault.SUBMIT_AMBIGUOUS_NOT_ACCEPTED)
    assert (await gateway.execute_intent(_buy())).status == "pending"
    assert await _intent_status(repositories) == TradeIntentStatus.SUBMISSION_UNKNOWN
    assert (await gateway.execute_intent(_buy())).status == "pending"
    assert broker._orders == {}

    # Accepted, and the recovery lookup itself times out: still never a
    # second order. The next attempt's lookup succeeds and adopts the one order.
    repositories, broker, gateway = await _gateway(tmp_path / "timeout")  # type: ignore[operator]
    broker.inject(Fault.SUBMIT_AMBIGUOUS_ACCEPTED)
    broker.inject(Fault.LOOKUP_TIMEOUT)
    assert (await gateway.execute_intent(_buy())).status == "pending"
    assert await _intent_status(repositories) == TradeIntentStatus.SUBMISSION_UNKNOWN
    assert len(broker._orders) == 1
    result = await gateway.execute_intent(_buy())
    assert (result.status, result.filled_quantity) == ("filled", Decimal("5"))
    assert await _intent_status(repositories) == TradeIntentStatus.FILLED
    assert len(broker._orders) == 1

    # A definitive rejection is final.
    repositories, broker, gateway = await _gateway(tmp_path / "rejected")  # type: ignore[operator]
    broker.inject(Fault.SUBMIT_REJECTED)
    assert (await gateway.execute_intent(_buy())).status == "rejected"
    assert await _intent_status(repositories) == TradeIntentStatus.REJECTED
    assert broker._orders == {}


async def test_disconnection_fails_exactly_the_network_calls_and_spares_local_ones() -> None:
    from tradepulse.broker.port import BrokerPort
    from tradepulse.broker.simulated import NETWORK_OPERATIONS

    # Every port call is either network-dependent or one of the three local ones.
    local = {"set_market_data_feeds", "rate_limit_snapshot", "aclose"}
    port_calls = set(BrokerPort.__protocol_attrs__) - {"venue", "implementation"}  # type: ignore[attr-defined]
    assert NETWORK_OPERATIONS == port_calls - local

    broker = _broker()
    broker.inject(Fault.DISCONNECTED)
    calls = {
        "get_account": broker.get_account(), "get_positions": broker.get_positions(), "get_clock": broker.get_clock(),
        "place_order": broker.place_order(_order("AAPL", "1")), "get_order": broker.get_order("x"),
        "get_order_by_client_order_id": broker.get_order_by_client_order_id("x"),
        "get_open_orders": broker.get_open_orders(), "cancel_order": broker.cancel_order("x"),
        "get_activities": broker.get_activities(), "get_latest_quote": broker.get_latest_quote("AAPL", AssetClass.EQUITY),
        "get_latest_option_quotes": broker.get_latest_option_quotes([OCC]),
        "get_options_chain": broker.get_options_chain("AAPL", "2026-10-01", "2026-12-31"),
        "get_bars": broker.get_bars("AAPL", AssetClass.EQUITY, T0, T0),
    }
    assert set(calls) == NETWORK_OPERATIONS
    for name, call in calls.items():
        exc = await _raises(call)
        assert isinstance(exc, httpx.ConnectError), name

    # Local configuration, telemetry and cleanup keep working during the outage.
    broker.set_market_data_feeds(equity_feed="sip", option_feed="opra")
    assert broker.rate_limit_snapshot is None
    await broker.aclose()
