"""DBAE at the execution gateway (spec §3, §4.4, §7, §8.1, §8.6, §9, §10, §11).

Every new buy is authorized inside the portfolio-risk lock against fresh
account and asset evidence, the session baseline and the funding still
committed to every pending buy; protective exits never read any of it.
Runs on the simulated broker with the always-in-session test calendar.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from lease_support import held_test_lease

from tradepulse.alerts import TelegramAlerter
from tradepulse.alerts.telegram import Severity
from tradepulse.broker import AlpacaError
from tradepulse.broker.capabilities import (
    AccountEvidence,
    AlpacaCapabilities,
    AssetTradingCapabilities,
    DailyEquityBar,
    NoCapabilityEvidence,
)
from tradepulse.broker.port import AccountRef
from tradepulse.broker.simulated import ManualClock, SimSession, SimulatedBroker, SimulatedCapabilities
from tradepulse.broker.types import AlpacaAccount, AlpacaOrderRequest, AlpacaOrderResponse, RawQuote
from tradepulse.calendar import BrokerSession, ny_date
from tradepulse.config import risk_limits_for_profile
from tradepulse.dbae.funding import max_funding, pending_buy_funding
from tradepulse.execution import PORTFOLIO_RISK_LOCK_KEY, ExecutionGateway, ExecutionRequest, ExecutionResult
from tradepulse.execution.fill_attribution import order_matches_intent
from tradepulse.execution.idempotency import release_symbol_reservation, reserve_symbol_for_execution
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    PositionLot,
    RiskLimits,
    SessionState,
    Side,
    TradeIntent,
    TradeIntentStatus,
    TradingSession,
)
from tradepulse.persistence import (
    AsyncSQLiteDatabase,
    DatabaseError,
    PersistenceRepositories,
    hydrate,
    list_all_by_json_field,
)
from tradepulse.persistence.codec import encode_payload
from tradepulse.persistence.repositories import RecordRepository
from tradepulse.providers import AlpacaMarketDataProvider
from tradepulse.reconciliation.bootstrap import bootstrap_fill_cursor
from tradepulse.reconciliation.coordinator import (
    STRANDED_INTENT_GRACE_SECONDS,
    _recover_inflight_orders,
    _recover_stranded_intents,
)
from tradepulse.risk import save_session
from tradepulse.risk.engine import FEE_PCT_PER_SIDE
from tradepulse.runtime_lease import account_lease
from tradepulse.settlement import SettlementProcessor

pytestmark = pytest.mark.usefixtures("open_market_calendar")

# The test calendar is centred on today's New York date; every day is a session.
NOW = datetime.combine(datetime.now(UTC).date(), time(15, 0), tzinfo=UTC)
FRAC = AssetIdentity("FRAC", AssetClass.EQUITY, "alpaca:FRAC")
FRAY = AssetIdentity("FRAY", AssetClass.EQUITY, "alpaca:FRAY")
FRAZ = AssetIdentity("FRAZ", AssetClass.EQUITY, "alpaca:FRAZ")
WHLE = AssetIdentity("WHLE", AssetClass.EQUITY, "alpaca:WHLE")
SOL = AssetIdentity("SOL/USD", AssetClass.CRYPTO, "alpaca:SOL/USD")
OPT = AssetIdentity("FRAC261218C00100000", AssetClass.OPTION, "alpaca:FRAC261218C00100000", contract_multiplier=100)
CRYPTO_MIN_ORDER = Decimal("0.01")
CRYPTO_INCREMENT = Decimal("0.001")
LATER = timedelta(days=1)  # a fill latency that keeps an order open for the whole test


def _account(balance: Decimal, at: datetime = NOW, **changes: Any) -> AccountEvidence:
    evidence = AccountEvidence(
        status="ACTIVE", trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
        suspend_trade=False, closing_transactions_only=False, fractional_trading=True,
        equity=balance, cash=balance, buying_power=balance, non_marginable_buying_power=balance,
        account_multiplier=Decimal(4), crypto_status="ACTIVE", options_trading_level=3, received_at=at)
    return replace(evidence, **changes)


def _assets(at: datetime = NOW) -> dict[str, AssetTradingCapabilities]:
    fractionable = AssetTradingCapabilities(True, True, True, None, None, None, at)
    return {
        FRAC.symbol: fractionable, FRAY.symbol: fractionable, FRAZ.symbol: fractionable,
        WHLE.symbol: AssetTradingCapabilities(True, True, False, None, None, None, at),
        SOL.symbol: AssetTradingCapabilities(True, True, True, CRYPTO_MIN_ORDER, CRYPTO_INCREMENT, None, at),
        OPT.symbol: AssetTradingCapabilities(True, True, False, None, None, None, at),
    }


class FreshCapabilities(SimulatedCapabilities):
    """Each read is stamped with the gateway clock, as a live read is, unless
    ``restamp`` is cleared: then the evidence is returned as stamped. The
    next account read can be made to stall until ``stall`` is set, and the
    reads are counted."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        super().__init__()
        self._clock = clock
        self.restamp = True
        self.stall: asyncio.Event | None = None
        self.stalled = asyncio.Event()
        self.reads: list[str] = []

    async def get_account_evidence(self) -> AccountEvidence:
        self.reads.append("account")
        gate, self.stall = self.stall, None
        if gate is not None:
            self.stalled.set()
            await gate.wait()
        evidence = await super().get_account_evidence()
        return replace(evidence, received_at=self._clock()) if self.restamp else evidence

    async def get_asset_capabilities(self, asset: AssetIdentity) -> AssetTradingCapabilities:
        self.reads.append(f"asset:{asset.symbol}")
        found = await super().get_asset_capabilities(asset)
        return replace(found, received_at=self._clock()) if self.restamp else found

    async def get_daily_equity_bars(self) -> list[DailyEquityBar]:
        self.reads.append("history")
        return await super().get_daily_equity_bars()


PlaceOrder = Callable[[AlpacaOrderRequest], Awaitable[AlpacaOrderResponse]]
LatestQuote = Callable[[str, AssetClass, "str | None"], Awaitable[RawQuote]]
GetAccount = Callable[[], Awaitable[AlpacaAccount]]
GetCalendar = Callable[[date, date], Awaitable[list[BrokerSession]]]


class HookedBroker(SimulatedBroker):
    """The simulated broker with typed seams a test can script. Each hook,
    when set, replaces the broker's own answer; ``place_on_book`` is the
    real submission, and records every order it places in ``orders``."""

    def __init__(self, *, clock: ManualClock, cash: Decimal) -> None:
        super().__init__(clock=clock, session=SimSession.always_open(), cash=cash, opening_journal=True)
        self.orders: list[AlpacaOrderRequest] = []
        self.place_hook: PlaceOrder | None = None
        self.quote_hook: LatestQuote | None = None
        self.account_hook: GetAccount | None = None
        self.calendar_hook: GetCalendar | None = None

    async def place_on_book(self, order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        self.orders.append(order)
        return await super().place_order(order)

    async def place_order(self, order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        if self.place_hook is not None:
            return await self.place_hook(order)
        return await self.place_on_book(order)

    async def real_latest_quote(self, symbol: str, asset_class: AssetClass,
                                feed_override: str | None = None) -> RawQuote:
        return await super().get_latest_quote(symbol, asset_class, feed_override)

    async def get_latest_quote(self, symbol: str, asset_class: AssetClass,
                               feed_override: str | None = None) -> RawQuote:
        if self.quote_hook is not None:
            return await self.quote_hook(symbol, asset_class, feed_override)
        return await self.real_latest_quote(symbol, asset_class, feed_override)

    async def real_account(self) -> AlpacaAccount:
        return await super().get_account()

    async def get_account(self) -> AlpacaAccount:
        if self.account_hook is not None:
            return await self.account_hook()
        return await self.real_account()

    async def real_calendar(self, start: date, end: date) -> list[BrokerSession]:
        return await super().get_calendar(start, end)

    async def get_calendar(self, start: date, end: date) -> list[BrokerSession]:
        if self.calendar_hook is not None:
            return await self.calendar_hook(start, end)
        return await self.real_calendar(start, end)


class RecordingAlerter(TelegramAlerter):
    """A configured alerter whose deliveries are recorded, never sent."""

    def __init__(self) -> None:
        super().__init__("token", "chat")
        self.delivered: list[str] = []

    async def send(self, severity: Severity, message: str, details: Mapping[str, Any] | None = None) -> bool:
        self.delivered.append(message)
        return True


class RecordingIntents(RecordRepository):
    """The trade-intent repository, recording the status and byte-exact
    ``dbae`` block of every update that carries one."""

    def __init__(self, database: AsyncSQLiteDatabase) -> None:
        super().__init__(database, "trade_intents")
        self.written: list[tuple[str, str]] = []

    async def update(
        self, record_id: str, payload: Any, *, status: str | None = None,
        guard_table: str | None = None, guard_key: str | None = None, lease: tuple[str, str] | None = None,
    ) -> bool:
        if isinstance(payload, TradeIntent) and "dbae" in payload.risk_snapshot:
            self.written.append((payload.status.value, encode_payload(payload.risk_snapshot["dbae"])))
        return await super().update(record_id, payload, status=status, guard_table=guard_table,
                                    guard_key=guard_key, lease=lease)


@dataclass
class Rig:
    path: Path
    database: AsyncSQLiteDatabase
    repositories: PersistenceRepositories
    broker: HookedBroker
    clock: ManualClock
    caps: FreshCapabilities
    alerts: TelegramAlerter
    limits: RiskLimits
    gateway: ExecutionGateway
    sent: list[tuple[str, str]] = field(default_factory=list)

    @property
    def orders(self) -> list[AlpacaOrderRequest]:
        """Every order the broker placed on its book."""
        return self.broker.orders

    def now(self) -> datetime:
        return self.clock() - timedelta(milliseconds=1)

    def quote(self, asset: AssetIdentity, price: Decimal) -> None:
        self.broker.set_quote(asset.symbol, asset.asset_class, price, price, at=self.clock())
        self.clock.advance(timedelta(seconds=1))

    def build_gateway(self) -> ExecutionGateway:
        return _gateway(self.repositories, self.broker, self.caps, self.alerts, self.limits, self.now)

    async def reopen(self) -> None:
        """A crash: the gateway and repositories are dropped and rebuilt over
        the same database file. The broker (the remote side) survives."""
        self.database = AsyncSQLiteDatabase(f"sqlite:///{self.path}")
        await self.database.initialize()
        self.repositories = PersistenceRepositories.create(self.database)
        self.gateway = self.build_gateway()

    async def intents(self) -> list[TradeIntent]:
        return [hydrate("trade_intents", row["payload"]) for row in await self.repositories.trade_intents.list_all()]

    async def pending(self) -> Decimal:
        return (await pending_buy_funding(self.repositories, self.limits)).total


def _gateway(repositories: PersistenceRepositories, broker: SimulatedBroker, caps: FreshCapabilities,
             alerts: TelegramAlerter, limits: RiskLimits, clock: Callable[[], datetime]) -> ExecutionGateway:
    settlement = SettlementProcessor(repositories, alerts, clock=clock)
    return ExecutionGateway(repositories, broker, AlpacaMarketDataProvider(broker), settlement, alerts, limits,
                            ExecutionMode.PAPER, clock=clock, lease=held_test_lease(), capabilities=caps)


async def _rig(tmp_path: Path, name: str = "dbae", *, balance: Decimal = Decimal(100000), profile: str = "balanced",
               alerts: TelegramAlerter | None = None) -> Rig:
    path = tmp_path / f"{name}.db"
    database = AsyncSQLiteDatabase(f"sqlite:///{path}")
    await database.initialize()
    repositories = PersistenceRepositories.create(database)
    clock = ManualClock(NOW)
    broker = HookedBroker(clock=clock, cash=balance)
    caps = FreshCapabilities(lambda: clock() - timedelta(milliseconds=1))
    caps.account = _account(balance)
    caps.assets = _assets()
    # The previous trading date's close agrees with the broker's last_equity (the starting cash).
    caps.bars = [DailyEquityBar(ny_date(NOW) - timedelta(days=n), balance) for n in range(14, 0, -1)]
    alerter = alerts or TelegramAlerter(None, None)
    limits = risk_limits_for_profile(profile)
    gateway = _gateway(repositories, broker, caps, alerter, limits, lambda: clock() - timedelta(milliseconds=1))
    rig = Rig(path, database, repositories, broker, clock, caps, alerter, limits, gateway)
    await save_session(repositories, TradingSession("session", SessionState.ACTIVE, True, NOW))
    return rig


def _buy(asset: AssetIdentity, quantity: Decimal, stop: Decimal | None, key: str | None = None) -> ExecutionRequest:
    return ExecutionRequest(asset=asset, side=Side.BUY, requested_quantity=quantity, strategy="test",
                            confidence=Decimal(100), stop_loss=stop, idempotency_key=key)


def _hold_orders_open(rig: Rig) -> None:
    """Orders stay accepted: they fill a day later and the gateway does not poll."""
    rig.broker.set_fill_latency(LATER)
    rig.gateway.FILL_TIMEOUT_SECONDS = 0


def _dbae(intent: TradeIntent) -> dict[str, Any]:
    block = intent.risk_snapshot["dbae"]
    assert isinstance(block, dict)
    return block


async def _intent(rig: Rig, trade_intent_id: str | None) -> TradeIntent:
    assert trade_intent_id is not None
    row = await rig.repositories.trade_intents.get(trade_intent_id)
    assert row is not None
    return hydrate("trade_intents", row["payload"])


async def _events(rig: Rig, event_type: str) -> list[dict[str, Any]]:
    return [row["payload"] for row in await list_all_by_json_field(rig.repositories.audit_events, "event_type", event_type)]


# --- the balance matrix (spec §4.1, §6, §8.2, §8.3, §8.6) -------------------------------------------------
#
# The expected outcome of each case is derived here from the profile limits
# (tradepulse/config/risk_profiles.py) with the arithmetic of the unchanged
# risk engine (risk/engine.py::evaluate_risk) on a fresh account: equity =
# cash = buying power = the balance, nothing held or pending, quotes with
# bid = ask (no spread, no slippage), confidence 100 (no scaling) and a stop
# at the profile's stop_loss_pct below the price. A request of 1,000 units
# leaves the caps to size every case. Then DBAE: the band table (§4.1), the
# round-down (§8.2), the re-approval of a changed quantity (§7.1) and exit
# feasibility (§8.3) with an empty sub-minimum evidence table.

BALANCES = ("20", "25", "50", "99.99", "100", "500", "1999.99", "2000")
PROFILES = ("micro", "conservative", "balanced", "aggressive")
KINDS = ("fractionable", "whole", "crypto", "option")
REQUEST = Decimal(1000)
_PRECISION = {AssetClass.EQUITY: Decimal(1000), AssetClass.CRYPTO: Decimal(100000000), AssetClass.OPTION: Decimal(1)}
_MIN_QUANTITY = {AssetClass.EQUITY: Decimal("0.001"), AssetClass.CRYPTO: Decimal("0.00000001"),
                 AssetClass.OPTION: Decimal(1)}


def _floor(quantity: Decimal, asset_class: AssetClass) -> Decimal:
    precision = _PRECISION[asset_class]
    return (max(quantity, Decimal(0)) * precision).to_integral_value(rounding=ROUND_FLOOR) / precision


def _sized(limits: RiskLimits, asset_class: AssetClass, equity: Decimal, funding: Decimal, price: Decimal,
           stop: Decimal, multiplier: Decimal, requested: Decimal) -> tuple[bool, Decimal, list[str]]:
    """evaluate_risk for a fresh account (see above), in the engine's order."""
    cost_pct = 2 * FEE_PCT_PER_SIDE.get(asset_class, Decimal(0))  # spread is zero
    risk_pct = (price - stop) / price * 100
    if cost_pct > limits.max_cost_to_risk_pct / 100 * risk_pct:
        reason = f"COST_EXCEEDS_RISK_FRACTION ({cost_pct:.2f}% > {limits.max_cost_to_risk_pct}% of R {risk_pct:.2f}%)"
        return False, Decimal(0), [reason]
    reasons: list[str] = []
    quantity = requested
    per_unit = price * multiplier
    unit_risk = (price - stop) * multiplier
    by_risk = _floor(limits.max_risk_per_trade_pct / 100 * equity / unit_risk, asset_class)
    if by_risk < quantity:
        quantity = by_risk
        reasons.append(f"POSITION_CAPPED_TO_{quantity}_BY_RISK_BASED_SIZING")
    by_open_risk = _floor(limits.max_open_risk_pct / 100 * equity / unit_risk, asset_class)
    if by_open_risk < quantity:
        quantity = by_open_risk
        reasons.append(f"POSITION_CAPPED_TO_{quantity}_BY_MAX_OPEN_RISK")
    position = limits.max_position_pct / 100 * equity
    if quantity * per_unit > position:
        quantity = _floor(position / per_unit, asset_class)
        reasons.append(f"POSITION_CAPPED_TO_{quantity}_BY_MAX_POSITION_PCT")
    for limit, label in ((limits.max_sector_pct, "MAX_SECTOR_PCT"), (limits.max_total_exposure_pct, "MAX_TOTAL_EXPOSURE")):
        headroom = limit / 100 * equity
        if quantity * per_unit > headroom and _floor(headroom / per_unit, asset_class) < quantity:
            quantity = _floor(headroom / per_unit, asset_class)
            reasons.append(f"POSITION_CAPPED_TO_{quantity}_BY_{label}")
    buffered = funding * Decimal("0.99")  # check_cash_sufficiency's 1% buffer, no fees passed
    if quantity * per_unit > buffered and _floor(buffered / per_unit, asset_class) < quantity:
        quantity = _floor(buffered / per_unit, asset_class)
        reasons.append(f"POSITION_CAPPED_TO_{quantity}_BY_AVAILABLE_CASH")
    if quantity < _MIN_QUANTITY[asset_class] or quantity * per_unit < limits.min_lot_notional:
        return False, Decimal(0), [*reasons, "INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT"]
    return True, quantity, reasons


def _band(balance: Decimal) -> str:
    for edge, band in ((Decimal(2000), "full"), (Decimal(500), "standard"), (Decimal(100), "fractional"),
                       (Decimal(50), "micro_plus"), (Decimal(20), "micro")):
        if balance >= edge:
            return band
    return "below_minimum"


_CASE_ASSET = {"fractionable": FRAC, "whole": WHLE, "crypto": SOL, "option": OPT}
_CASE_PRICE = {"fractionable": Decimal(100), "whole": Decimal(100), "crypto": Decimal(100), "option": Decimal(1)}


def _expected(balance: Decimal, profile: str, kind: str) -> tuple[str, Decimal | None, list[str] | None]:
    """(status, submitted quantity, reasons) for one case."""
    limits = risk_limits_for_profile(profile)
    asset = _CASE_ASSET[kind]
    price = _CASE_PRICE[kind]
    stop = price * (1 - limits.stop_loss_pct / 100)
    multiplier = Decimal(100) if kind == "option" else Decimal(1)
    band = _band(balance)
    if kind == "whole" and band in ("micro", "micro_plus"):
        return "rejected", None, ["DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND"]
    if kind == "crypto" and band in ("micro", "micro_plus"):
        return "rejected", None, ["DBAE_ASSET_CLASS_LOCKED"]
    if kind == "option" and band != "full":
        return "rejected", None, ["DBAE_ASSET_CLASS_LOCKED"]
    approved, quantity, reasons = _sized(limits, asset.asset_class, balance, balance, price, stop, multiplier, REQUEST)
    if not approved:
        return "rejected", None, reasons
    if kind == "whole":
        shaped = quantity.to_integral_value(rounding=ROUND_FLOOR)
        if shaped == 0:
            return "rejected", None, ["WHOLE_SHARE_UNAFFORDABLE"]
    elif kind == "crypto":
        shaped = (quantity // CRYPTO_INCREMENT) * CRYPTO_INCREMENT
        if shaped < CRYPTO_MIN_ORDER:
            return "rejected", None, ["DBAE_BELOW_ASSET_MINIMUM"]
    else:
        shaped = quantity  # fractionable equity keeps the engine's 0.001; options are already whole
    if shaped != quantity:
        again, reapproved, _ = _sized(limits, asset.asset_class, balance, balance, price, stop, multiplier, shaped)
        if not again or reapproved != shaped:
            return "rejected", None, ["DBAE_SHAPED_QUANTITY_NOT_REAPPROVED"]
    threshold = max(Decimal(1), CRYPTO_MIN_ORDER * stop) if kind == "crypto" else Decimal(1)
    if shaped * stop * multiplier < threshold:
        return "rejected", None, ["DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED"]
    return "filled", shaped, None


def test_matrix_expectations_match_the_spec_20_dollar_cases() -> None:
    """§8.6: the derivation above reproduces the spec's worked $20 cases."""
    assert _expected(Decimal(20), "micro", "fractionable") == ("filled", Decimal("0.033"), None)  # ~$3.33
    assert _expected(Decimal(20), "conservative", "fractionable") == (
        "rejected", None, ["DBAE_SUB_MINIMUM_LIQUIDATION_UNVERIFIED"])  # $1.00, $0.95 at the stop
    status, _, reasons = _expected(Decimal(20), "balanced", "fractionable")
    assert status == "rejected" and reasons is not None and reasons[-1] == "INSUFFICIENT_CAPACITY_FOR_MINIMUM_LOT"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("balance", BALANCES)
async def test_balance_matrix(tmp_path: Path, balance: str, profile: str, kind: str) -> None:
    rig = await _rig(tmp_path, balance=Decimal(balance), profile=profile)
    asset = _CASE_ASSET[kind]
    price = _CASE_PRICE[kind]
    rig.quote(asset, price)
    stop = price * (1 - rig.limits.stop_loss_pct / 100)

    result = await rig.gateway.execute_intent(_buy(asset, REQUEST, stop))

    status, submitted, reasons = _expected(Decimal(balance), profile, kind)
    assert result.status == status, result.reasons
    [intent] = await rig.intents()
    if submitted is not None:
        assert result.filled_quantity == submitted
        assert intent.requested_quantity == submitted
        assert Decimal(_dbae(intent)["submitted_quantity"]) == submitted
        assert [order.qty for order in rig.orders] == [submitted]
    else:
        assert result.reasons == reasons
        assert intent.status == TradeIntentStatus.REJECTED and intent.rejection_reason == "; ".join(reasons or [])
        assert "dbae" not in intent.risk_snapshot
        assert rig.orders == []  # no case is approved after a refusal, by evaluate_risk or by DBAE
        assert await rig.pending() == 0


# --- funding and the four quantities (spec §7) -------------------------------------------------------------


async def test_request_20_approved_3_50_reserves_order_only(tmp_path: Path) -> None:
    """§7.2: a $20 request approved at $3.50 reserves $3.50 plus its
    allowance, never $20. micro at $20: risk 1% = $0.20 over a $0.40 unit
    risk (stop $6.60 under a $7 ask) is 0.5 shares = $3.50 (cap 20% = $4)."""
    rig = await _rig(tmp_path, balance=Decimal(20), profile="micro")
    _hold_orders_open(rig)
    rig.quote(FRAC, Decimal(7))
    request = Decimal("2.857")  # $19.999 at $7

    result = await rig.gateway.execute_intent(_buy(FRAC, request, Decimal("6.60")))

    assert result.status == "pending"
    [intent] = await rig.intents()
    dbae = _dbae(intent)
    assert (dbae["original_requested_quantity"], dbae["risk_approved_quantity"]) == ("2.857", "0.5")
    assert (dbae["shaped_quantity"], dbae["submitted_quantity"]) == ("0.5", "0.5")
    reserved = Decimal("0.5") * 7 * (1 + rig.limits.slippage_limit_pct / 100)  # equities: no buy-side fee
    assert Decimal(dbae["max_funding"]) == reserved == Decimal("3.535")
    assert await rig.pending() == reserved
    assert await rig.pending() < request * 7


async def test_quantity_identity_survives_restart(tmp_path: Path) -> None:
    """Request 10; micro at $7,300 risks 1% = $73 over a $10 unit risk:
    risk-approved 7.3, shaped and submitted 7 whole shares."""
    rig = await _rig(tmp_path, balance=Decimal(7300), profile="micro")
    rig.quote(WHLE, Decimal(100))
    intents = RecordingIntents(rig.database)
    rig.repositories = replace(rig.repositories, trade_intents=intents)
    rig.gateway = rig.build_gateway()
    rig.gateway.FILL_TIMEOUT_SECONDS = 0.3
    rig.gateway.POLL_INTERVAL_SECONDS = 0.01
    written = intents.written

    async def scripted(order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        # 3 now, the other 4 a day later: the order is partially filled when the poll ends.
        rig.broker.schedule_fills(order.client_order_id or "", [(timedelta(0), Decimal(3), None),
                                                                (LATER, Decimal(4), None)])
        return await rig.broker.place_on_book(order)

    rig.broker.place_hook = scripted

    result = await rig.gateway.execute_intent(_buy(WHLE, Decimal(10), Decimal(90)))

    assert result.status == "pending" and result.filled_quantity == Decimal(3)
    statuses = [status for status, _ in written]
    assert statuses == ["risk_approved", "submitted", "accepted", "partially_filled", "partially_filled"]
    assert len({block for _, block in written}) == 1  # byte-identical through every later update

    trade_intent_id = result.trade_intent_id
    await rig.reopen()  # a new repository instance over the same file
    intent = await _intent(rig, trade_intent_id)
    dbae = _dbae(intent)
    assert {key: dbae[key] for key in ("original_requested_quantity", "risk_approved_quantity", "shaped_quantity",
                                       "submitted_quantity")} == {
        "original_requested_quantity": "10", "risk_approved_quantity": "7.3", "shaped_quantity": "7",
        "submitted_quantity": "7"}
    assert intent.requested_quantity == Decimal(7)
    order = await rig.broker.get_order_by_client_order_id(intent.trade_intent_id)
    assert order is not None and Decimal(order.raw["qty"]) == Decimal(7) and order_matches_intent(order, intent)
    pending = await pending_buy_funding(rig.repositories, rig.limits)
    assert pending.mismatches == () and pending.unpriced == ()
    assert pending.total == max_funding(Decimal(7) - 3, Decimal(100), WHLE, rig.limits.slippage_limit_pct)


async def test_shaping_that_breaks_min_lot_is_not_reapproved(tmp_path: Path) -> None:
    """0.0199 SOL at $90 ($1.79) floors to the 0.01 increment: $0.90 is under
    the $1 minimum lot, so the second evaluate_risk refuses it."""
    rig = await _rig(tmp_path, balance=Decimal(100000), profile="balanced")
    rig.caps.assets[SOL.symbol] = AssetTradingCapabilities(True, True, True, Decimal("0.01"), Decimal("0.01"), None, NOW)
    rig.quote(SOL, Decimal(90))

    result = await rig.gateway.execute_intent(_buy(SOL, Decimal("0.0199"), Decimal(80)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_SHAPED_QUANTITY_NOT_REAPPROVED"])
    assert rig.orders == []
    [intent] = await rig.intents()
    assert intent.rejection_reason == "DBAE_SHAPED_QUANTITY_NOT_REAPPROVED"


async def test_second_evaluation_uses_same_quote_and_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review Focus 5: the re-approval reuses the entry's quote, snapshot and
    options. A second market-data read would see another price."""
    import tradepulse.execution.gateway as gateway_module

    rig = await _rig(tmp_path, balance=Decimal(7300), profile="micro")
    rig.quote(WHLE, Decimal(100))
    quote_calls: list[str] = []

    async def moving_quote(symbol: str, asset_class: AssetClass, feed_override: str | None = None) -> RawQuote:
        quote_calls.append(symbol)
        raw = await rig.broker.real_latest_quote(symbol, asset_class, feed_override)
        if len(quote_calls) == 1 or raw.bid is None or raw.ask is None:
            return raw
        return replace(raw, bid=raw.bid * 2, ask=raw.ask * 2)

    rig.broker.quote_hook = moving_quote
    evaluations: list[tuple[Any, Any, Any]] = []
    real_evaluate = gateway_module.evaluate_risk

    def recording_evaluate(risk_input: Any, snapshot: Any, limits: Any, opts: Any = None) -> Any:
        evaluations.append((risk_input, snapshot, opts))
        return real_evaluate(risk_input, snapshot, limits, opts)

    monkeypatch.setattr(gateway_module, "evaluate_risk", recording_evaluate)

    result = await rig.gateway.execute_intent(_buy(WHLE, Decimal(10), Decimal(90)))

    assert result.status == "filled" and result.filled_quantity == Decimal(7)
    assert quote_calls == ["WHLE"]  # exactly once per entry
    [(first, snapshot, opts), (second, snapshot_again, opts_again)] = evaluations
    assert snapshot_again is snapshot and opts_again is opts
    assert second == replace(first, requested_quantity=Decimal(7)) and second.price == Decimal(100)
    [intent] = await rig.intents()
    assert _dbae(intent)["reapproved"] is True


@pytest.mark.parametrize("cash", ["20", "5"])
async def test_two_concurrent_buys_different_symbols_20_cash(tmp_path: Path, cash: str) -> None:
    """A $20 micro account. The first buy is approved and its order stays
    accepted; the second, gathered with it, decides after the first's
    approval and sizes against funding_available = cash - P. With $20 of
    cash the risk budget binds both; with $5 of cash the second is capped
    by the funding the first left."""
    rig = await _rig(tmp_path, balance=Decimal(20), profile="micro")
    funds = Decimal(cash)
    rig.caps.account = _account(Decimal(20), cash=funds, buying_power=funds, non_marginable_buying_power=funds)
    _hold_orders_open(rig)
    rig.quote(FRAC, Decimal(100))
    rig.quote(FRAY, Decimal(100))
    release = rig.broker.hold_call("place_order")  # the first holds its submission open

    async def first() -> ExecutionResult:
        return await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(94)))

    async def second() -> ExecutionResult:
        while not rig.orders:  # the first has committed its approval and is submitting
            await asyncio.sleep(0)
        try:
            return await rig.gateway.execute_intent(_buy(FRAY, Decimal(1), Decimal(94)))
        finally:
            release()

    a, b = await asyncio.gather(first(), second())

    assert (a.status, b.status) == ("pending", "pending"), (a.reasons, b.reasons)
    first_intent = await _intent(rig, a.trade_intent_id)
    second_intent = await _intent(rig, b.trade_intent_id)
    assert first_intent.status == second_intent.status == TradeIntentStatus.ACCEPTED
    first_dbae, second_dbae = _dbae(first_intent), _dbae(second_intent)
    first_funding = Decimal(first_dbae["max_funding"])
    assert Decimal(first_dbae["pending_buy_funding"]) == 0
    assert Decimal(second_dbae["pending_buy_funding"]) == first_funding
    available = Decimal(second_dbae["funding_available"])
    assert available == funds - first_funding
    second_quantity = Decimal(second_dbae["submitted_quantity"])
    assert second_quantity * 100 <= available * Decimal("0.99")  # check_cash_sufficiency's 1% buffer
    capped = any(r.endswith("_BY_AVAILABLE_CASH") for r in second_intent.risk_snapshot["reasons"])
    if cash == "5":
        assert capped and second_quantity < Decimal(first_dbae["submitted_quantity"])
    else:
        assert not capped and second_quantity == Decimal(first_dbae["submitted_quantity"])
    total = first_funding + Decimal(second_dbae["max_funding"])
    assert total <= funds
    assert await rig.pending() == total


async def test_restart_after_ambiguous_buy_no_second_buy(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, balance=Decimal(100000))
    rig.quote(FRAC, Decimal(100))
    calls: list[str] = []

    async def unavailable(order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        calls.append(order.client_order_id or "")
        raise AlpacaError("service unavailable", 503, None, None, "placeOrder")  # never reached the book

    rig.broker.place_hook = unavailable
    request = _buy(FRAC, Decimal(5), Decimal(92), key="ambiguous-buy")

    result = await rig.gateway.execute_intent(request)

    assert result.status == "pending" and result.reasons[0].startswith("SUBMISSION_UNKNOWN")
    before = await rig.pending()
    [intent] = await rig.intents()
    assert intent.status == TradeIntentStatus.SUBMISSION_UNKNOWN
    assert before == Decimal(_dbae(intent)["max_funding"]) > 0

    await rig.reopen()
    rig.broker.place_hook = None
    rig.quote(FRAC, Decimal(100))
    again = await rig.gateway.execute_intent(request)

    assert again.status == "pending"
    assert await rig.pending() == before
    assert len(calls) == 1 and rig.broker._orders == {}


# --- the portfolio-risk lease (Review Focus 4, DBAE-P1, DBAE-P2) ----------------------------------------


async def _expire_portfolio_lease(rig: Rig) -> None:
    def expire(connection: Any) -> None:  # the wall clock has passed the lease's TTL
        connection.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00+00:00' WHERE lock_key=?",
                           (PORTFOLIO_RISK_LOCK_KEY,))

    await rig.repositories.trade_intents.database.run(expire, write=True)


async def test_lease_lost_before_approval_writes_nothing(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    rig.quote(FRAC, Decimal(100))
    rig.caps.stall = stall = asyncio.Event()
    task = asyncio.create_task(rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92))))
    await asyncio.wait_for(rig.caps.stalled.wait(), timeout=10)
    await _expire_portfolio_lease(rig)
    stall.set()
    result = await asyncio.wait_for(task, timeout=30)

    assert (result.status, result.reasons) == ("skipped", ["PORTFOLIO_RISK_LOCK_LOST"])
    [intent] = await rig.intents()
    assert intent.status == TradeIntentStatus.REJECTED and "dbae" not in intent.risk_snapshot
    assert await rig.pending() == 0
    assert rig.orders == []


async def test_stale_worker_cannot_commit_after_takeover(tmp_path: Path) -> None:
    """DBAE-P1: worker A stalls at its account read inside the lock; the
    lease lapses; worker B takes the lock, approves Y and commits; A then
    tries to approve X."""
    rig = await _rig(tmp_path)
    _hold_orders_open(rig)
    rig.quote(FRAC, Decimal(100))
    rig.quote(FRAY, Decimal(100))
    rig.caps.stall = stall = asyncio.Event()
    worker_a = asyncio.create_task(rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92))))
    await asyncio.wait_for(rig.caps.stalled.wait(), timeout=10)
    await _expire_portfolio_lease(rig)

    b = await rig.gateway.execute_intent(_buy(FRAY, Decimal(5), Decimal(92)))
    stall.set()
    a = await asyncio.wait_for(worker_a, timeout=30)

    assert b.status == "pending"
    assert (a.status, a.reasons) == ("skipped", ["PORTFOLIO_RISK_LOCK_LOST"])
    funded = [i for i in await rig.intents() if i.status != TradeIntentStatus.REJECTED]
    assert [i.asset.symbol for i in funded] == ["FRAY"]  # only B's intent was ever approved
    a_intent = await _intent(rig, a.trade_intent_id)
    assert "dbae" not in a_intent.risk_snapshot
    assert await rig.pending() == Decimal(_dbae(funded[0])["max_funding"])
    assert [order.symbol for order in rig.orders] == ["FRAY"]  # the order route was called once


async def test_funding_conserved_across_crash_and_restart(tmp_path: Path) -> None:
    """DBAE-P2: P is recomputed from disk after a crash, and drops only by a
    broker-confirmed terminal state. aggressive allows five simultaneous
    orders, so C is not refused for the two still pending."""
    rig = await _rig(tmp_path, profile="aggressive")
    rig.gateway.FILL_TIMEOUT_SECONDS = 0.3
    rig.gateway.POLL_INTERVAL_SECONDS = 0.01
    rig.broker.set_fill_latency(LATER)

    async def scripted(order: AlpacaOrderRequest) -> AlpacaOrderResponse:
        if order.symbol == FRAY.symbol:  # the submission times out before Alpaca records anything
            raise httpx.ReadTimeout("simulated: submit timed out")
        if order.symbol == FRAC.symbol:  # 6 of 10 now, the rest a day later
            rig.broker.schedule_fills(order.client_order_id or "", [(timedelta(0), Decimal(6), None),
                                                                    (LATER, Decimal(4), None)])
        return await rig.broker.place_on_book(order)

    rig.broker.place_hook = scripted
    for asset in (FRAC, FRAY, FRAZ):
        rig.quote(asset, Decimal(100))

    a = await rig.gateway.execute_intent(_buy(FRAC, Decimal(10), Decimal(92)))  # 1: partially filled
    assert (a.status, a.filled_quantity) == ("pending", Decimal(6))
    b = await rig.gateway.execute_intent(_buy(FRAY, Decimal(10), Decimal(92)))  # 2: submission unknown
    assert b.status == "pending" and b.reasons[0].startswith("SUBMISSION_UNKNOWN")

    await rig.reopen()  # 3: crash
    slippage = rig.limits.slippage_limit_pct
    a_intent = await _intent(rig, a.trade_intent_id)
    b_intent = await _intent(rig, b.trade_intent_id)
    a_remaining = max_funding(Decimal(4), a_intent.reference_price or Decimal(0), FRAC, slippage)
    b_full = Decimal(_dbae(b_intent)["max_funding"])
    assert b_full == max_funding(Decimal(10), b_intent.reference_price or Decimal(0), FRAY, slippage)
    assert await rig.pending() == a_remaining + b_full

    rig.gateway.FILL_TIMEOUT_SECONDS = 0
    rig.quote(FRAZ, Decimal(100))
    c = await rig.gateway.execute_intent(_buy(FRAZ, Decimal(10), Decimal(92)))  # 4
    assert c.status == "pending", c.reasons
    c_dbae = _dbae(await _intent(rig, c.trade_intent_id))
    assert Decimal(c_dbae["pending_buy_funding"]) == a_remaining + b_full
    assert Decimal(c_dbae["funding_available"]) == Decimal(100000) - (a_remaining + b_full)
    assert Decimal(c_dbae["max_funding"]) <= Decimal(c_dbae["funding_available"])
    c_funding = Decimal(c_dbae["max_funding"])
    assert await rig.pending() == a_remaining + b_full + c_funding

    # B: reconciliation proves Alpaca never received it.
    rig.clock.advance(timedelta(seconds=STRANDED_INTENT_GRACE_SECONDS + 1))
    assert await _recover_stranded_intents(rig.repositories, rig.broker, rig.alerts, rig.now()) == 1
    assert (await _intent(rig, b.trade_intent_id)).status == TradeIntentStatus.REJECTED
    assert await rig.pending() == a_remaining + c_funding

    # A: canceled at the broker with 6 filled; reconciliation confirms the terminal state.
    a_order = a_intent.broker_order_id
    assert a_order is not None
    await rig.broker.cancel_order(a_order)
    settlement = SettlementProcessor(rig.repositories, rig.alerts, clock=rig.now)
    await _recover_inflight_orders(rig.repositories, rig.broker, settlement, rig.alerts, rig.now())
    assert (await _intent(rig, a.trade_intent_id)).status == TradeIntentStatus.CANCELED
    assert await rig.pending() == c_funding


@pytest.mark.parametrize(("profile", "balance", "asset", "price", "stop"), [
    ("aggressive", Decimal(1000), FRAC, Decimal(10), Decimal("9.90")),  # 1.5% allowance, no fee
    ("balanced", Decimal(2000), SOL, Decimal(100), Decimal(94)),  # 1% allowance plus 0.25% crypto fee
])
async def test_cash_bound_buy_reserves_within_funding_available(tmp_path: Path, profile: str, balance: Decimal,
                                                                asset: AssetIdentity, price: Decimal,
                                                                stop: Decimal) -> None:
    """Task 10 property-test counterexample: with $50 of funding the cash
    cap binds. Sized against funding_available itself, the engine approved
    $49.50 of notional (its 1% buffer), and max_funding added the slippage
    allowance (and crypto's fee) on top: $50.24 reserved against $50.00
    available on the aggressive equity case. The engine now sizes against
    fundable_notional, so the reservation stays within the funding."""
    rig = await _rig(tmp_path, balance=balance, profile=profile)
    funds = Decimal(50)
    rig.caps.account = _account(balance, cash=funds, buying_power=funds, non_marginable_buying_power=funds)
    _hold_orders_open(rig)
    rig.quote(asset, price)

    result = await rig.gateway.execute_intent(_buy(asset, Decimal(1000), stop))

    assert result.status == "pending", result.reasons
    intent = await _intent(rig, result.trade_intent_id)
    dbae = _dbae(intent)
    assert Decimal(dbae["funding_available"]) == funds
    assert any(reason.endswith("_BY_AVAILABLE_CASH") for reason in intent.risk_snapshot["reasons"])
    assert Decimal(dbae["max_funding"]) <= funds
    assert await rig.pending() <= funds


# --- integrity, audit and failure handling (spec §4.4, §7.1, §10, §11) -------------------------------------


async def test_identity_mismatch_alerts_once_and_refuses_new_entries(tmp_path: Path) -> None:
    alerts = RecordingAlerter()
    rig = await _rig(tmp_path, alerts=alerts)
    delivered = alerts.delivered
    bad = TradeIntent("bad-intent", "idem-bad", "corr-bad", FRAY, Side.BUY, ExecutionMode.PAPER, "test", NOW,
                      requested_quantity=Decimal(7), reference_price=Decimal(100), status=TradeIntentStatus.ACCEPTED,
                      broker_order_id="order-bad", risk_snapshot={"dbae": {"submitted_quantity": "5"}})
    await rig.repositories.trade_intents.create_once("bad-intent", bad, status=bad.status.value, unique_value="idem-bad")
    rig.quote(FRAC, Decimal(100))

    first = await rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92), key="one"))
    rig.quote(FRAC, Decimal(100))
    second = await rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92), key="two"))

    for result in (first, second):
        assert (result.status, result.reasons) == ("rejected", ["DBAE_QUANTITY_IDENTITY_MISMATCH"])
    assert rig.orders == []
    events = await _events(rig, "dbae_quantity_identity_mismatch")
    assert len(events) == 1 and events[0]["severity"] == "critical" and events[0]["entity_id"] == "bad-intent"
    assert len([m for m in delivered if "DBAE_QUANTITY_IDENTITY_MISMATCH" in m]) == 1


async def test_band_changed_event_once_per_change(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    for _ in range(2):
        rig.quote(FRAC, Decimal(100))
        assert (await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))).status == "filled"
    rig.caps.account = _account(Decimal(1000))  # a fresh reading in a lower band: downgraded at once (§4.2)
    for _ in range(2):
        rig.quote(FRAC, Decimal(100))
        await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))

    events = await _events(rig, "dbae_band_changed")
    assert [(e["details"]["old_band"], e["details"]["new_band"]) for e in events] == [(None, "full"), ("full", "standard")]
    details = events[1]["details"]
    assert Decimal(details["band_equity"]) == Decimal(1000) and Decimal(details["current_equity"]) == Decimal(1000)
    assert Decimal(details["previous_close_equity"]) == Decimal(100000)
    assert details["session_date"] == ny_date(NOW).isoformat()
    assert details["unlocked_classes"] == ["equity", "crypto"]


async def test_refusal_writes_reason_and_event(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, balance=Decimal(40))
    rig.caps.account = _account(Decimal(40), fractional_trading=None)
    rig.quote(WHLE, Decimal(10))

    result = await rig.gateway.execute_intent(_buy(WHLE, Decimal(1), Decimal(9)))

    reasons = ["DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND", "DBAE_FRACTIONAL_TRADING_UNVERIFIED"]
    assert (result.status, result.reasons) == ("rejected", reasons)
    [intent] = await rig.intents()
    assert intent.status == TradeIntentStatus.REJECTED
    assert intent.rejection_reason == "DBAE_WHOLE_SHARES_NOT_ALLOWED_IN_BAND; DBAE_FRACTIONAL_TRADING_UNVERIFIED"
    [event] = await _events(rig, "dbae_entry_refused")
    details = event["details"]
    assert details["reasons"] == reasons and details["band"] == "micro" and Decimal(details["band_equity"]) == 40
    assert details["asset_class"] == "equity"
    assert details["account_evidence_age_seconds"] is not None and details["asset_evidence_age_seconds"] is not None
    assert event["entity_id"] == intent.trade_intent_id


# --- a store that cannot be read refuses the entry; it never raises (final review I2, spec §10) ----------------


class UnreadableBaselines(RecordRepository):
    """The session-baseline table, failing every read as a locked or
    corrupt database would."""

    def __init__(self, database: AsyncSQLiteDatabase) -> None:
        super().__init__(database, "dbae_session_baselines")

    async def get(self, record_id: str) -> Mapping[str, Any] | None:
        raise DatabaseError("simulated: dbae_session_baselines unreadable")


async def _unreadable(*args: object, **kwargs: object) -> Any:
    raise DatabaseError("simulated: store unreadable")


async def test_baseline_store_failure_refuses_entry(tmp_path: Path) -> None:
    """_baseline_capture_due's read fails: the entry is refused as an
    unverified baseline, not raised."""
    rig = await _rig(tmp_path)
    rig.repositories = replace(rig.repositories, dbae_session_baselines=UnreadableBaselines(rig.database))
    rig.gateway = rig.build_gateway()
    rig.quote(FRAC, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_SESSION_BASELINE_UNVERIFIED"])
    assert rig.orders == []
    [intent] = await rig.intents()
    assert intent.rejection_reason == "DBAE_SESSION_BASELINE_UNVERIFIED"


async def test_pending_funding_store_failure_refuses_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P cannot be read: funding is unverified, and the entry is refused."""
    import tradepulse.execution.gateway as gateway_module

    rig = await _rig(tmp_path)
    monkeypatch.setattr(gateway_module, "pending_buy_funding", _unreadable)
    rig.quote(FRAC, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_FUNDING_UNVERIFIED"])
    assert rig.orders == []


async def test_band_audit_store_failure_does_not_block_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dbae_band_changed is audit, not a decision input: a failed read is
    logged and the approved entry goes on."""
    import tradepulse.execution.gateway as gateway_module

    rig = await _rig(tmp_path)
    monkeypatch.setattr(gateway_module, "list_all_by_json_field", _unreadable)
    rig.quote(FRAC, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))

    assert result.status == "filled", result.reasons
    assert await _events(rig, "dbae_band_changed") == []


async def test_identity_mismatch_alert_store_failure_still_refuses(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import tradepulse.alerts.once as once_module

    rig = await _rig(tmp_path)
    bad = TradeIntent("bad-intent", "idem-bad", "corr-bad", FRAY, Side.BUY, ExecutionMode.PAPER, "test", NOW,
                      requested_quantity=Decimal(7), reference_price=Decimal(100), status=TradeIntentStatus.ACCEPTED,
                      broker_order_id="order-bad", risk_snapshot={"dbae": {"submitted_quantity": "5"}})
    await rig.repositories.trade_intents.create_once("bad-intent", bad, status=bad.status.value, unique_value="idem-bad")
    monkeypatch.setattr(once_module, "alert_once", _unreadable)
    rig.quote(FRAC, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(5), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_QUANTITY_IDENTITY_MISMATCH"])
    assert rig.orders == []


# --- protective exits never read DBAE evidence (spec §9) --------------------------------------------------


@dataclass
class ExitRig:
    rig: Rig
    token: str = "exit-token"

    async def stop_exit(self, quantity: Decimal) -> ExecutionResult:
        database = self.rig.repositories.trade_intents.database
        assert await reserve_symbol_for_execution(database, FRAC, self.token)
        try:
            return await self.rig.gateway.execute_intent(ExecutionRequest(
                asset=FRAC, side=Side.SELL, requested_quantity=quantity, strategy="position_monitor",
                decision_id="stop-1", exit_reason="stop_loss", symbol_lock_owner_token=self.token))
        finally:
            await release_symbol_reservation(database, FRAC, self.token)


@asynccontextmanager
async def _held(tmp_path: Path, balance: Decimal) -> AsyncIterator[ExitRig]:
    """An account holding 0.05 FRAC, with the fill cursor bootstrapped."""
    rig = await _rig(tmp_path, balance=balance)
    with account_lease(AccountRef("alpaca", "SIM0001"), holder="dbae-exit", directory=tmp_path) as lease:
        await bootstrap_fill_cursor(rig.repositories, rig.broker, lease, reason="test", now=rig.clock())
        rig.quote(FRAC, Decimal(100))
        rig.broker.seed_position(FRAC.symbol, AssetClass.EQUITY, Decimal("0.05"), Decimal(100))
        lot = PositionLot(lot_id="lot-frac", originating_fill_id="fill-frac", asset=FRAC, position_side="long",
                          opened_quantity=Decimal("0.05"), remaining_quantity=Decimal("0.05"),
                          acquisition_price=Decimal(100), opened_at=NOW)
        await rig.repositories.position_lots.create_once(lot.lot_id, lot, unique_value=lot.originating_fill_id)
        rig.clock.advance(timedelta(seconds=1))
        rig.quote(FRAC, Decimal(80))
        yield ExitRig(rig)


async def test_protective_exit_skips_dbae(tmp_path: Path) -> None:
    """The account is at $10 (BELOW_MINIMUM) and every DBAE input is unknown."""
    async with _held(tmp_path, Decimal(10)) as held:
        await _exit_without_dbae(held)


async def _exit_without_dbae(held: ExitRig) -> None:
    rig = held.rig
    rig.caps.account = None  # every DBAE input unknown: each read raises
    rig.caps.assets = {}
    rig.caps.bars = []

    result = await held.stop_exit(Decimal("0.05"))

    assert result.status == "filled", result.reasons
    assert not any(reason.startswith("DBAE") for reason in result.reasons)
    assert [(order.symbol, order.side, order.qty) for order in rig.orders] == [("FRAC", Side.SELL, Decimal("0.05"))]
    assert rig.caps.reads == []
    [intent] = await rig.intents()
    assert "dbae" not in intent.risk_snapshot and not (intent.rejection_reason or "").startswith("DBAE")
    assert await _events(rig, "dbae_entry_refused") == []


async def test_capability_read_failure_is_unknown(tmp_path: Path) -> None:
    """An AlpacaError from the asset read is unknown evidence: the entry is
    refused under the asset's own reason, and the exit path is unaffected."""
    async with _held(tmp_path, Decimal(100000)) as held:
        rig = held.rig
        del rig.caps.assets[FRAY.symbol]  # the asset read raises AlpacaError (404)
        rig.quote(FRAY, Decimal(100))

        entry = await rig.gateway.execute_intent(_buy(FRAY, Decimal(1), Decimal(92)))

        assert (entry.status, entry.reasons) == ("rejected", ["DBAE_ASSET_NOT_TRADABLE"])
        assert rig.orders == []
        exit_result = await held.stop_exit(Decimal("0.05"))
        assert exit_result.status == "filled", exit_result.reasons
        assert [(order.symbol, order.side) for order in rig.orders] == [("FRAC", Side.SELL)]


async def test_account_read_failure_refuses_entry(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    rig.caps.account = None
    rig.quote(FRAC, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_ACCOUNT_STATE_UNVERIFIED"])
    assert rig.orders == []


def test_build_gateway_capability_source() -> None:
    from tradepulse.broker import AlpacaClient
    from tradepulse.broker.capabilities import capabilities_for

    client = AlpacaClient("key", "secret", "paper", 10)
    assert isinstance(capabilities_for(client), AlpacaCapabilities)
    simulated = SimulatedBroker(clock=ManualClock(NOW), session=SimSession.always_open())
    assert isinstance(capabilities_for(simulated), NoCapabilityEvidence)


# --- the kill switch is never pre-empted by a DBAE refusal (fix round 1) ----------------------------------


async def _daily_loss_rig(tmp_path: Path, start: Decimal, now_equity: Decimal) -> Rig:
    """balanced (max daily loss 1%) on an account whose equity has fallen
    from ``start`` (the previous close, last_equity) to ``now_equity``."""
    rig = await _rig(tmp_path, balance=start, profile="balanced")
    broker = rig.broker

    async def fallen() -> AlpacaAccount:
        account = await broker.real_account()
        return replace(account, equity=now_equity, cash=now_equity, portfolio_value=now_equity)

    broker.account_hook = fallen
    rig.caps.account = _account(now_equity)
    rig.quote(FRAC, Decimal(10))
    return rig


async def _latched(rig: Rig) -> list[dict[str, Any]]:
    from tradepulse.risk import load_session

    session = await load_session(rig.repositories)
    assert session.state == SessionState.RISK_STOPPED and session.kill_switch_reset_required is True
    assert (session.kill_switch_reason or "").startswith("MAX_DAILY_LOSS_EXCEEDED")
    return [e for e in await _events(rig, "session_transition") if e["details"]["action"] == "latch_risk_stop"]


async def test_daily_loss_latches_behind_a_below_minimum_refusal(tmp_path: Path) -> None:
    """A $25 account at $18: the band is BELOW_MINIMUM and the daily loss is 28%."""
    rig = await _daily_loss_rig(tmp_path, Decimal(25), Decimal(18))

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(9)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_BELOW_MINIMUM_BALANCE"])
    [latch] = await _latched(rig)
    assert latch["severity"] == "critical" and latch["details"]["new_state"] == "risk_stopped"
    assert rig.orders == []


async def test_daily_loss_latches_behind_a_funding_refusal(tmp_path: Path) -> None:
    rig = await _daily_loss_rig(tmp_path, Decimal(100000), Decimal(98000))
    rig.caps.account = _account(Decimal(98000), cash=Decimal(-1))  # a negative funding input

    result = await rig.gateway.execute_intent(_buy(FRAC, Decimal(1), Decimal(9)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_FUNDING_UNVERIFIED"])
    [latch] = await _latched(rig)
    assert latch["severity"] == "critical"
    assert rig.orders == []


async def test_dbae_refusal_without_a_loss_does_not_latch(tmp_path: Path) -> None:
    from tradepulse.risk import load_session

    rig = await _rig(tmp_path, balance=Decimal(40))
    rig.quote(SOL, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(SOL, Decimal(1), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_ASSET_CLASS_LOCKED"])
    assert (await load_session(rig.repositories)).state == SessionState.ACTIVE
    assert await _events(rig, "session_transition") == []


# --- no baseline step for unusable evidence; no calendar call once the baseline is stored ------------------


async def test_stale_account_evidence_refused_before_the_baseline_step(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    rig.caps.restamp = False  # the evidence is returned as stamped: an hour old
    rig.caps.account = _account(Decimal(100000), at=NOW - timedelta(hours=1))
    rig.caps.assets = _assets()
    calendar_calls: list[object] = []
    broker = rig.broker

    async def counted(start: date, end: date) -> list[BrokerSession]:
        calendar_calls.append((start, end))
        return await broker.real_calendar(start, end)

    broker.calendar_hook = counted
    rig.quote(SOL, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(SOL, Decimal(1), Decimal(92)))

    assert (result.status, result.reasons) == ("rejected", ["DBAE_ACCOUNT_STATE_UNVERIFIED"])
    assert calendar_calls == []
    assert await rig.repositories.dbae_session_baselines.list_all() == []
    assert await _events(rig, "dbae_session_baseline_captured") == []
    assert await _events(rig, "dbae_session_baseline_unverified") == []


async def test_no_calendar_call_once_the_baseline_is_stored(tmp_path: Path) -> None:
    """The session's baseline is already stored (say, by an earlier process
    whose calendar check this database lacks): a crypto buy makes no broker
    calendar call inside the lock."""
    from tradepulse.dbae.baseline import SessionBaseline

    rig = await _rig(tmp_path)
    today = ny_date(NOW)
    stored = SessionBaseline(session_date=today, previous_trading_date=today - timedelta(days=1),
                             previous_close_equity=Decimal(100000), last_equity=Decimal(100000),
                             source="portfolio_history", captured_at=NOW)
    await rig.repositories.dbae_session_baselines.create_once(today.isoformat(), stored)
    calendar_calls: list[object] = []

    async def unreachable(start: date, end: date) -> list[BrokerSession]:
        calendar_calls.append((start, end))
        raise httpx.ConnectError("calendar unavailable")

    rig.broker.calendar_hook = unreachable
    rig.quote(SOL, Decimal(100))

    result = await rig.gateway.execute_intent(_buy(SOL, Decimal(1), Decimal(92)))

    assert result.status == "filled", result.reasons
    assert calendar_calls == []
    assert "history" not in rig.caps.reads
