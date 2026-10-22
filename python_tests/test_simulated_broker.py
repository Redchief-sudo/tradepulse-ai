"""SimulatedBroker foundation (Phase A0 Task 4, Rev.133): manual clock,
exchange calendar, as-of market data and account arithmetic."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tradepulse.broker import AlpacaError
from tradepulse.broker.port import BrokerPort, account_ref
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker, SimulatorScriptError
from tradepulse.broker.types import AlpacaOptionContract, AlpacaOrderRequest, RawBar
from tradepulse.models import AssetClass, Side

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


# --- order surface before Rev.134 ------------------------------------------


async def test_order_reads_report_an_empty_book_and_submission_is_refused() -> None:
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
    with pytest.raises(NotImplementedError):
        await broker.place_order(AlpacaOrderRequest(symbol="AAPL", qty=Decimal("1"), side=Side.BUY))


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
