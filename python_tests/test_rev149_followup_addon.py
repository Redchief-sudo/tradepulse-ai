"""Rev.149 follow-up R149-05 (review 2026-11-05): max_open_positions limits
distinct instruments, so an add-on to an asset that already holds a slot is
not refused by the position cap.

Operator ruling 22 (docs/rev149-record.md): the cap counts distinct assets,
which is what the snapshot already reports. Before the fix, evaluate_risk
refused every BUY once open_positions reached the cap, including an add-on
that creates no new position. Daily-entry, per-symbol, open-risk, sector,
notional and cash limits still apply to an add-on.
"""
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
import respx
from dbae_support import FullBandCapabilities
from lease_support import held_test_lease
from test_execution_gateway import (
    NOW,
    _aapl,
    _mock_account,
    _mock_market_open,
    _mock_positions,
    _mock_quote,
    _position_json,
    _setup,
)
from test_rev149_risk_counts import (
    AAPL,
    MSFT,
    TIGHT,
    _holding,
    _intent,
    _msft_buy,
    _opts,
    _position,
    _repositories,
)

from tradepulse.alerts import TelegramAlerter
from tradepulse.config import risk_limits_for_profile
from tradepulse.execution import ExecutionGateway, ExecutionRequest
from tradepulse.models import (
    ExecutionMode,
    Holding,
    SessionState,
    Side,
    TradeIntentStatus,
    TradingSession,
    asset_identity_key,
)
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.risk import build_portfolio_view, evaluate_risk, save_session
from tradepulse.settlement import SettlementProcessor

AT_CAP = replace(TIGHT, max_daily_trades=10, max_simultaneous_orders=10)  # max_open_positions=1


async def _view(repositories, positions):
    by_key = {asset_identity_key(p_asset): p for p_asset, p in positions}
    return await build_portfolio_view(
        repositories, cash_balance=Decimal(50000), account_equity=Decimal(100000),
        broker_prev_close_equity=Decimal(100000), mark_prices={k: p.current_price for k, p in by_key.items()},
        broker_positions=by_key, now=NOW)


def _decide(view, candidate=MSFT, limits=AT_CAP, **opts):
    holds_slot = asset_identity_key(candidate) in view.position_keys
    return evaluate_risk(_msft_buy(), view.snapshot, limits, replace(_opts(), candidate_holds_slot=holds_slot, **opts))


async def test_an_add_on_to_a_held_asset_at_the_cap_is_not_refused_for_slots(tmp_path) -> None:
    """The review's probe: one MSFT share held, a cap of one, one more MSFT."""
    repositories = await _repositories(tmp_path)
    await _holding(repositories, MSFT, "1")
    view = await _view(repositories, [(MSFT, _position(MSFT, "1", "100", "100"))])

    decision = _decide(view)

    assert view.snapshot.open_positions == 1
    assert decision.approved, decision.reasons


async def test_a_different_symbol_at_the_cap_is_still_refused(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _holding(repositories, AAPL, "1")
    view = await _view(repositories, [(AAPL, _position(AAPL, "1", "100", "100"))])

    decision = _decide(view)

    assert not decision.approved
    assert any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


async def test_an_add_on_to_a_pending_entry_at_the_cap_is_not_refused_for_slots(tmp_path) -> None:
    """MSFT's slot is held by an approved, unfilled entry."""
    repositories = await _repositories(tmp_path)
    await _intent(repositories, "ti-msft", MSFT, TradeIntentStatus.RISK_APPROVED)
    view = await _view(repositories, [])

    decision = _decide(view)

    assert view.snapshot.open_positions == 1
    assert not any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


async def test_an_add_on_at_the_cap_is_still_bound_by_its_other_limits(tmp_path) -> None:
    """The add-on escapes only the slot rule. Its
    per-symbol entry limit still refuses it, and with no cash it is still refused for cash."""
    repositories = await _repositories(tmp_path)
    await _holding(repositories, MSFT, "1")
    view = await _view(repositories, [(MSFT, _position(MSFT, "1", "100", "100"))])

    by_entries = _decide(view, symbol_entries_today=AT_CAP.max_entries_per_symbol_per_day)
    by_cash = _decide(view, available_cash=Decimal(0))

    assert any(r.startswith("MAX_ENTRIES_PER_SYMBOL_REACHED") for r in by_entries.reasons)
    assert not by_cash.approved
    for decision in (by_entries, by_cash):
        assert not any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


async def test_without_slot_evidence_the_cap_refuses_as_before(tmp_path) -> None:
    """candidate_holds_slot defaults to False: a caller that does not know
    whether the asset holds a slot gets the conservative refusal."""
    repositories = await _repositories(tmp_path)
    await _holding(repositories, MSFT, "1")
    view = await _view(repositories, [(MSFT, _position(MSFT, "1", "100", "100"))])

    decision = evaluate_risk(_msft_buy(), view.snapshot, AT_CAP, _opts())

    assert any(r.startswith("MAX_OPEN_POSITIONS_REACHED") for r in decision.reasons)


@pytest.mark.usefixtures("open_market_calendar")
@respx.mock
async def test_the_gateway_sends_an_add_on_at_the_position_cap(tmp_path) -> None:
    """End to end: a held AAPL position, a cap of one position, an AAPL
    add-on. Before the fix it was refused with MAX_OPEN_POSITIONS_REACHED
    and no order was sent."""
    repositories, broker, _ = await _setup(tmp_path)
    limits = replace(risk_limits_for_profile("balanced"), max_open_positions=1)
    gateway = ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker),
                               SettlementProcessor(repositories, TelegramAlerter(None, None), clock=lambda: NOW),
                               TelegramAlerter(None, None), limits, ExecutionMode.PAPER, clock=lambda: NOW,
                               lease=held_test_lease(), capabilities=FullBandCapabilities(broker, lambda: NOW))
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    holding = Holding(_aapl(), Decimal(5), Decimal(150), NOW, sector="Technology", stop_loss=Decimal(150))
    await repositories.holdings.create_once(asset_identity_key(_aapl()), holding)
    _mock_account()
    _mock_positions(_position_json(qty="5", current_price="199.55"))
    _mock_quote()
    _mock_market_open()
    order_route = respx.post("https://paper-api.alpaca.markets/v2/orders").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://paper-api.alpaca.markets/v2/orders:by_client_order_id").mock(return_value=httpx.Response(404))

    await gateway.execute_intent(ExecutionRequest(
        asset=_aapl(), side=Side.BUY, requested_quantity=Decimal(1), strategy="test",
        confidence=Decimal(100), stop_loss=Decimal(190), sector="Technology"))
    await broker.aclose()

    [row] = await repositories.trade_intents.list_all()
    assert "MAX_OPEN_POSITIONS_REACHED" not in (row["payload"].get("rejection_reason") or "")
    assert order_route.call_count == 1
