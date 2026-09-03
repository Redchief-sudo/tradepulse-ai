import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradepulse.alerts import TelegramAlerter
from tradepulse.models import (
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    MarketQuote,
    Opportunity,
    PositionLot,
    SessionState,
    SettlementEvent,
    SettlementStatus,
    Side,
    TradeIntent,
    asset_identity_key,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate
from tradepulse.risk import load_session
from tradepulse.settlement import SettlementProcessor


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def asset() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


def _no_op_alerter() -> TelegramAlerter:
    return TelegramAlerter(bot_token=None, chat_id=None)  # no creds -- send() no-ops, never raises


async def _seed_buy(repositories: PersistenceRepositories, *, fill_id: str = "fill-1", quantity: str = "10", price: str = "150") -> None:
    intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal(quantity),
    )
    await repositories.trade_intents.create_once("ti-1", intent, status=intent.status.value, unique_value=intent.idempotency_key)

    fill = Fill(fill_id, "ti-1", "order-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once(fill_id, fill, unique_value=None)

    event = SettlementEvent("se-1", fill_id, "ti-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), NOW)
    await repositories.settlements.create_once("se-1", event, status=event.status.value, unique_value=fill_id)


async def test_buy_settlement_projects_lot_holding_and_intent_summary(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    summary = await processor.process_pending()

    assert summary.processed == 1
    assert summary.completed == 1
    assert summary.ok

    lot_rows = await repositories.position_lots.list_all()
    assert len(lot_rows) == 1
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.position_side == "long"
    assert lot.remaining_quantity == Decimal("10")

    holding_row = await repositories.holdings.get(asset_identity_key(asset()))
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.quantity == Decimal("10")
    assert holding.average_price == Decimal("150")

    intent_row = await repositories.trade_intents.get("ti-1")
    intent = hydrate("trade_intents", intent_row["payload"])
    assert intent.filled_quantity == Decimal("10")
    assert intent.filled_avg_price == Decimal("150")

    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.COMPLETED.value
    event = hydrate("settlements", event_row["payload"])
    assert event.integrity_verified


async def test_replaying_a_completed_event_is_a_no_op(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    await processor.process_pending()
    second_summary = await processor.process_pending()

    assert second_summary.processed == 0  # COMPLETED events are not processable again
    lot_rows = await repositories.position_lots.list_all()
    assert len(lot_rows) == 1  # no duplicate lot created by the replay


async def test_sell_closes_the_long_lot_and_realizes_pnl(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    sell_intent = TradeIntent(
        "ti-2", "idem-2", "corr-2", asset(), Side.SELL, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"),
    )
    await repositories.trade_intents.create_once("ti-2", sell_intent, status=sell_intent.status.value, unique_value=sell_intent.idempotency_key)
    sell_fill = Fill("fill-2", "ti-2", "order-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-2", sell_fill, unique_value=None)
    sell_event = SettlementEvent("se-2", "fill-2", "ti-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), NOW)
    await repositories.settlements.create_once("se-2", sell_event, status=sell_event.status.value, unique_value="fill-2")

    summary = await processor.process_pending()
    assert summary.completed == 1

    holding_row = await repositories.holdings.get(asset_identity_key(asset()))
    assert holding_row is None  # fully closed position -- the Holding row must be gone, not zero-quantity

    lot_rows = await repositories.position_lots.list_all()
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.status == "closed"
    assert lot.realized_pnl == Decimal("150")  # (165-150)*10


def _aapl_call() -> AssetIdentity:
    return AssetIdentity(
        "AAPL251219C00150000", AssetClass.OPTION, "alpaca:AAPL251219C00150000",
        metadata={"underlying_symbol": "AAPL", "contract_multiplier": "100"},
    )


async def test_option_realized_pnl_applies_the_contract_multiplier_end_to_end(tmp_path) -> None:
    """The exact accounting invariant from plan review: BUY 1 contract at
    $2.00 premium (multiplier 100, entry notional $200), SELL at $2.50 --
    realized gross P&L must equal EXACTLY $50 ((2.50-2.00)*1*100) all the
    way through Fill -> SettlementEvent -> lot consumption -> realized P&L,
    never $0.50 (multiplier forgotten), $5,000 (applied twice), or $200
    (premium mistaken for P&L). The single cheapest test against the most
    dangerous silent accounting failure options introduce."""
    repositories = await _repositories(tmp_path)
    contract = _aapl_call()

    buy_intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", contract, Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("1"),
    )
    await repositories.trade_intents.create_once("ti-1", buy_intent, status=buy_intent.status.value, unique_value=buy_intent.idempotency_key)
    buy_fill = Fill("fill-1", "ti-1", "order-1", contract, Side.BUY, ExecutionMode.PAPER, Decimal("1"), Decimal("2.00"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-1", buy_fill, unique_value=None)
    buy_event = SettlementEvent("se-1", "fill-1", "ti-1", contract, Side.BUY, ExecutionMode.PAPER, Decimal("1"), Decimal("2.00"), NOW)
    await repositories.settlements.create_once("se-1", buy_event, status=buy_event.status.value, unique_value="fill-1")

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    holding_row = await repositories.holdings.get(asset_identity_key(contract))
    assert holding_row is not None
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.average_price == Decimal("2.00")  # a per-unit PRICE, never multiplied -- the multiplier only ever applies at the notional/P&L boundary

    sell_intent = TradeIntent(
        "ti-2", "idem-2", "corr-2", contract, Side.SELL, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("1"),
    )
    await repositories.trade_intents.create_once("ti-2", sell_intent, status=sell_intent.status.value, unique_value=sell_intent.idempotency_key)
    sell_fill = Fill("fill-2", "ti-2", "order-2", contract, Side.SELL, ExecutionMode.PAPER, Decimal("1"), Decimal("2.50"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-2", sell_fill, unique_value=None)
    sell_event = SettlementEvent("se-2", "fill-2", "ti-2", contract, Side.SELL, ExecutionMode.PAPER, Decimal("1"), Decimal("2.50"), NOW)
    await repositories.settlements.create_once("se-2", sell_event, status=sell_event.status.value, unique_value="fill-2")

    summary = await processor.process_pending()
    assert summary.completed == 1

    holding_row = await repositories.holdings.get(asset_identity_key(contract))
    assert holding_row is None  # fully closed

    lot_rows = await repositories.position_lots.list_all()
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.status == "closed"
    assert lot.realized_pnl == Decimal("50")  # (2.50 - 2.00) * 1 * 100 -- NOT 0.50, 5000, or 200

    settlement_event_row = await repositories.settlements.get("se-2")
    settlement_event = hydrate("settlements", settlement_event_row["payload"])
    assert settlement_event.realized_pnl == Decimal("50")

    intent_row = await repositories.trade_intents.get("ti-2")
    intent = hydrate("trade_intents", intent_row["payload"])
    assert intent.realized_pnl == Decimal("50")


async def test_manufactured_integrity_violation_blocks_permanently_and_alerts(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    # A lot that already claims to have closed more than the incoming event's
    # own quantity for the same fill_id -- an impossible state that only a
    # data-corruption bug could produce, exactly what verify/lot-planning
    # must catch.
    corrupted_lot = PositionLot(
        "lot-x", "origin-x", asset(), "long", Decimal("10"), Decimal("10"), Decimal("100"), NOW,
        closures={"fill-bad": Decimal("999")},
    )
    await repositories.position_lots.create_once("lot-x", corrupted_lot, unique_value="origin-x")

    intent = TradeIntent("ti-3", "idem-3", "corr-3", asset(), Side.SELL, ExecutionMode.PAPER, "manual", NOW, requested_quantity=Decimal("5"))
    await repositories.trade_intents.create_once("ti-3", intent, status=intent.status.value, unique_value=intent.idempotency_key)
    bad_event = SettlementEvent("se-3", "fill-bad", "ti-3", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("5"), Decimal("100"), NOW)
    await repositories.settlements.create_once("se-3", bad_event, status=bad_event.status.value, unique_value="fill-bad")

    alerts = _no_op_alerter()
    processor = SettlementProcessor(repositories, alerts, clock=lambda: NOW)
    summary = await processor.process_pending()

    assert summary.integrity_blocked == 1
    assert not summary.ok

    event_row = await repositories.settlements.get("se-3")
    assert event_row["status"] == SettlementStatus.INTEGRITY_BLOCKED.value
    event = hydrate("settlements", event_row["payload"])
    assert event.next_retry_at is None  # permanent -- never auto-retried
    assert event.error_code is not None and "OVERALLOCATED" in event.error_code

    # A second run must not retry an integrity_blocked event either.
    second_summary = await processor.process_pending()
    assert second_summary.processed == 0

    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED  # latched, not just alerted
    assert session.financial_integrity_manual_reenable_required is True
    events = await repositories.audit_events.list_all(limit=10)
    assert len(events) == 1


async def test_project_trade_realized_pnl_is_recomputed_not_accumulated_on_replay(tmp_path) -> None:
    from tradepulse.settlement.engine import _project_trade

    repositories = await _repositories(tmp_path)
    intent = TradeIntent(
        "ti-9", "idem-9", "corr-9", asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"),
    )
    await repositories.trade_intents.create_once("ti-9", intent, status=intent.status.value, unique_value=intent.idempotency_key)

    # Two settlement events for the same intent, each with its own stable
    # realized_pnl -- exactly what project_lot would have set, once, on a
    # prior run (see settlement/lots.py's closures-dict replay protection).
    event_a = SettlementEvent(
        "se-a", "fill-a", "ti-9", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("5"), Decimal("110"), NOW,
        realized_pnl=Decimal("50"),
    )
    event_b = SettlementEvent(
        "se-b", "fill-b", "ti-9", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("5"), Decimal("120"), NOW,
        realized_pnl=Decimal("100"),
    )
    await repositories.settlements.create_once("se-a", event_a, status=event_a.status.value, unique_value="fill-a")
    await repositories.settlements.create_once("se-b", event_b, status=event_b.status.value, unique_value="fill-b")

    await _project_trade(repositories, event_a)
    intent_row = await repositories.trade_intents.get("ti-9")
    assert hydrate("trade_intents", intent_row["payload"]).realized_pnl == Decimal("150")

    # Replay -- regression guard: must recompute the same total (150), never
    # accumulate a second 150 on top of it.
    await _project_trade(repositories, event_a)
    intent_row = await repositories.trade_intents.get("ti-9")
    assert hydrate("trade_intents", intent_row["payload"]).realized_pnl == Decimal("150")


async def test_realized_pnl_is_stable_across_a_full_replay_of_completed_settlement(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    sell_intent = TradeIntent(
        "ti-10", "idem-10", "corr-10", asset(), Side.SELL, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"),
    )
    await repositories.trade_intents.create_once("ti-10", sell_intent, status=sell_intent.status.value, unique_value=sell_intent.idempotency_key)
    sell_fill = Fill("fill-10", "ti-10", "order-10", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-10", sell_fill, unique_value=None)
    sell_event = SettlementEvent("se-10", "fill-10", "ti-10", asset(), Side.SELL, ExecutionMode.PAPER, Decimal("10"), Decimal("165"), NOW)
    await repositories.settlements.create_once("se-10", sell_event, status=sell_event.status.value, unique_value="fill-10")

    await processor.process_pending()

    event_row = await repositories.settlements.get("se-10")
    lot_rows = await repositories.position_lots.list_all()
    first_event_pnl = hydrate("settlements", event_row["payload"]).realized_pnl
    first_lot_pnl = hydrate("position_lots", lot_rows[0]["payload"]).realized_pnl

    # All events are already COMPLETED -- nothing should reprocess.
    second_summary = await processor.process_pending()
    assert second_summary.processed == 0

    event_row_again = await repositories.settlements.get("se-10")
    lot_rows_again = await repositories.position_lots.list_all()
    assert hydrate("settlements", event_row_again["payload"]).realized_pnl == first_event_pnl
    assert hydrate("position_lots", lot_rows_again[0]["payload"]).realized_pnl == first_lot_pnl


def _claim_decide(owner: str):
    def decide(current: SettlementEvent) -> SettlementEvent | None:
        if current.status != SettlementStatus.PENDING:
            return None
        return replace(current, status=SettlementStatus.PROCESSING, processing_owner=owner, processing_started_at=NOW)

    return decide


async def test_claim_if_processable_prevents_a_second_sequential_claim(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)  # creates se-1, PENDING

    first = await repositories.settlements.claim_if_processable("se-1", _claim_decide("worker-a"))
    assert first is not None
    assert first.status == SettlementStatus.PROCESSING
    assert first.processing_owner == "worker-a"

    second = await repositories.settlements.claim_if_processable("se-1", _claim_decide("worker-b"))
    assert second is None  # already claimed -- decide() sees status=PROCESSING and refuses


async def test_claim_if_processable_true_concurrency_race_exactly_one_wins(tmp_path) -> None:
    """Not just sequential calls -- two coroutines racing via asyncio.gather
    against the SAME AsyncSQLiteDatabase. Each database.run(write=True) call
    executes on its own thread (asyncio.to_thread), so these genuinely
    contend for SQLite's BEGIN IMMEDIATE write lock rather than merely
    interleaving cooperatively."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)

    results = await asyncio.gather(
        repositories.settlements.claim_if_processable("se-1", _claim_decide("worker-a")),
        repositories.settlements.claim_if_processable("se-1", _claim_decide("worker-b")),
    )
    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]
    assert len(successes) == 1
    assert len(failures) == 1


async def test_process_pending_reclaims_a_stale_processing_event(tmp_path) -> None:
    """A crashed processor that claimed an event but never checkpointed
    further must not permanently strand it."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    event_row = await repositories.settlements.get("se-1")
    event = hydrate("settlements", event_row["payload"])
    stale = replace(
        event, status=SettlementStatus.PROCESSING, processing_owner="dead-worker",
        processing_started_at=NOW - timedelta(seconds=300),
    )
    await repositories.settlements.update("se-1", stale, status=stale.status.value)

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    summary = await processor.process_pending(stale_lease_seconds=120)

    assert summary.completed == 1
    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.COMPLETED.value


async def test_process_pending_leaves_a_recently_claimed_processing_event_alone(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)
    event_row = await repositories.settlements.get("se-1")
    event = hydrate("settlements", event_row["payload"])
    recent = replace(
        event, status=SettlementStatus.PROCESSING, processing_owner="live-worker",
        processing_started_at=NOW - timedelta(seconds=5),
    )
    await repositories.settlements.update("se-1", recent, status=recent.status.value)

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    summary = await processor.process_pending(stale_lease_seconds=120)

    assert summary.processed == 0
    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.PROCESSING.value


async def test_holding_protective_thresholds_are_first_entry_wins_across_a_second_fill(tmp_path) -> None:
    repositories = await _repositories(tmp_path)

    first_intent = TradeIntent(
        "ti-1", "idem-1", "corr-1", asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"), stop_loss=Decimal("140"), target_price=Decimal("170"),
    )
    await repositories.trade_intents.create_once("ti-1", first_intent, status=first_intent.status.value, unique_value=first_intent.idempotency_key)
    first_fill = Fill("fill-1", "ti-1", "order-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("150"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-1", first_fill, unique_value=None)
    first_event = SettlementEvent("se-1", "fill-1", "ti-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("150"), NOW)
    await repositories.settlements.create_once("se-1", first_event, status=first_event.status.value, unique_value="fill-1")

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    holding = hydrate("holdings", (await repositories.holdings.get(asset_identity_key(asset())))["payload"])
    assert holding.stop_loss == Decimal("140")
    assert holding.target_price == Decimal("170")

    later = NOW + timedelta(minutes=5)
    second_intent = TradeIntent(
        "ti-2", "idem-2", "corr-2", asset(), Side.BUY, ExecutionMode.PAPER, "manual", later,
        requested_quantity=Decimal("10"), stop_loss=Decimal("108"), target_price=Decimal("130"),
    )
    await repositories.trade_intents.create_once("ti-2", second_intent, status=second_intent.status.value, unique_value=second_intent.idempotency_key)
    second_fill = Fill("fill-2", "ti-2", "order-2", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("120"), Decimal("0"), Decimal("0"), later)
    await repositories.fills.create_once("fill-2", second_fill, unique_value=None)
    second_event = SettlementEvent("se-2", "fill-2", "ti-2", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("120"), later)
    await repositories.settlements.create_once("se-2", second_event, status=second_event.status.value, unique_value="fill-2")

    processor2 = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: later)
    await processor2.process_pending()

    holding = hydrate("holdings", (await repositories.holdings.get(asset_identity_key(asset())))["payload"])
    assert holding.quantity == Decimal("20")
    assert holding.average_price == Decimal("135")
    # first-entry-wins: the SECOND fill's thresholds must not overwrite the first's
    assert holding.stop_loss == Decimal("140")
    assert holding.target_price == Decimal("170")


def _aapl_crypto() -> AssetIdentity:
    """Same ticker text as asset() (AAPL equity) -- proves settlement keys
    Holdings/lots by canonical instrument identity, not display symbol."""
    return AssetIdentity("AAPL", AssetClass.CRYPTO, "alpaca:AAPLUSD")


async def test_two_fills_sharing_ticker_text_across_asset_classes_settle_independently(tmp_path) -> None:
    repositories = await _repositories(tmp_path)

    equity_intent = TradeIntent(
        "ti-equity", "idem-equity", "corr-equity", asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("10"),
    )
    await repositories.trade_intents.create_once("ti-equity", equity_intent, status=equity_intent.status.value, unique_value="idem-equity")
    equity_fill = Fill("fill-equity", "ti-equity", "order-equity", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("150"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-equity", equity_fill, unique_value=None)
    equity_event = SettlementEvent("se-equity", "fill-equity", "ti-equity", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("10"), Decimal("150"), NOW)
    await repositories.settlements.create_once("se-equity", equity_event, status=equity_event.status.value, unique_value="fill-equity")

    crypto_intent = TradeIntent(
        "ti-crypto", "idem-crypto", "corr-crypto", _aapl_crypto(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("3"),
    )
    await repositories.trade_intents.create_once("ti-crypto", crypto_intent, status=crypto_intent.status.value, unique_value="idem-crypto")
    crypto_fill = Fill("fill-crypto", "ti-crypto", "order-crypto", _aapl_crypto(), Side.BUY, ExecutionMode.PAPER, Decimal("3"), Decimal("60000"), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once("fill-crypto", crypto_fill, unique_value=None)
    crypto_event = SettlementEvent("se-crypto", "fill-crypto", "ti-crypto", _aapl_crypto(), Side.BUY, ExecutionMode.PAPER, Decimal("3"), Decimal("60000"), NOW)
    await repositories.settlements.create_once("se-crypto", crypto_event, status=crypto_event.status.value, unique_value="fill-crypto")

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    summary = await processor.process_pending()

    assert summary.completed == 2

    lot_rows = await repositories.position_lots.list_all()
    assert len(lot_rows) == 2  # not merged into one lot despite sharing ticker text

    equity_holding_row = await repositories.holdings.get(asset_identity_key(asset()))
    crypto_holding_row = await repositories.holdings.get(asset_identity_key(_aapl_crypto()))
    assert equity_holding_row is not None and crypto_holding_row is not None

    equity_holding = hydrate("holdings", equity_holding_row["payload"])
    crypto_holding = hydrate("holdings", crypto_holding_row["payload"])
    assert equity_holding.quantity == Decimal("10")
    assert crypto_holding.quantity == Decimal("3")


async def test_settlement_exhausting_retries_latches_financial_integrity(tmp_path, monkeypatch) -> None:
    """Finding 2: a settlement that permanently fails for a REASON OTHER
    THAN a detected integrity violation must still latch financial
    integrity once retries are exhausted -- a real broker fill (this event
    originates from one) whose accounting can never be completed leaves the
    local ledger unresolved, same severity as a detected violation."""
    from tradepulse.settlement.stages import MAX_SETTLEMENT_ATTEMPTS

    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)

    async def _boom(repositories: PersistenceRepositories, event: SettlementEvent) -> None:
        raise RuntimeError("synthetic non-integrity settlement failure")

    monkeypatch.setattr("tradepulse.settlement.engine._project_lot", _boom)

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    for _ in range(MAX_SETTLEMENT_ATTEMPTS):
        await processor.process_pending(force_retry=True)

    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.TERMINAL_FAILED.value
    event = hydrate("settlements", event_row["payload"])
    assert event.next_retry_at is None  # exhausted -- never retried again

    session = await load_session(repositories)
    assert session.state == SessionState.FINANCIAL_INTEGRITY_BLOCKED
    assert "permanently failed" in session.financial_integrity_reason


async def test_settlement_retryable_failure_does_not_latch_financial_integrity(tmp_path, monkeypatch) -> None:
    """A single transient failure (attempts not yet exhausted) must NOT
    latch the whole system -- only genuinely exhausted retries escalate."""
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories)

    async def _boom(repositories: PersistenceRepositories, event: SettlementEvent) -> None:
        raise RuntimeError("synthetic transient settlement failure")

    monkeypatch.setattr("tradepulse.settlement.engine._project_lot", _boom)

    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending(force_retry=True)  # attempt 1 of 8 -- not exhausted

    event_row = await repositories.settlements.get("se-1")
    assert event_row["status"] == SettlementStatus.RETRYABLE_FAILED.value

    session = await load_session(repositories)
    assert session.state != SessionState.FINANCIAL_INTEGRITY_BLOCKED


# ---- Outcome Attribution ---------------------------------------------------


def _quote() -> MarketQuote:
    return MarketQuote(asset(), Decimal("150"), NOW, NOW, "test", 1)


async def _seed_buy_with_protective_levels(
    repositories: PersistenceRepositories, *, fill_id: str = "fill-1", quantity: str = "10", price: str = "150",
    stop_loss: Decimal | None = None, target_price: Decimal | None = None, opportunity_id: str | None = None,
    max_hold_days: int | None = None,
) -> None:
    """Like _seed_buy, but a scanner-shaped ("ai_scan") intent carrying
    protective levels and risk_snapshot provenance -- and, if opportunity_id
    is given, a linked Opportunity row too -- so attribution's entry_context
    and exit_reason inference have real data to work with."""
    risk_snapshot: dict[str, str] = {"regime": "low_vol_bull", "confidence": "90"}
    if max_hold_days is not None:
        risk_snapshot["max_hold_days"] = str(max_hold_days)
    intent = TradeIntent(
        "ti-1", "idem-1", opportunity_id or "corr-1", asset(), Side.BUY, ExecutionMode.PAPER, "ai_scan", NOW,
        requested_quantity=Decimal(quantity), stop_loss=stop_loss, target_price=target_price,
        risk_snapshot=risk_snapshot,
    )
    await repositories.trade_intents.create_once("ti-1", intent, status=intent.status.value, unique_value=intent.idempotency_key)
    fill = Fill(fill_id, "ti-1", "order-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), Decimal("0"), Decimal("0"), NOW)
    await repositories.fills.create_once(fill_id, fill, unique_value=None)
    event = SettlementEvent("se-1", fill_id, "ti-1", asset(), Side.BUY, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), NOW)
    await repositories.settlements.create_once("se-1", event, status=event.status.value, unique_value=fill_id)
    if opportunity_id is not None:
        opportunity = Opportunity(opportunity_id, "gen-1", asset(), _quote(), "anthropic", NOW, confidence=90, metadata={"composite_score": "82"})
        await repositories.opportunities.create_once(opportunity_id, opportunity)


async def _seed_sell(
    repositories: PersistenceRepositories, *, quantity: str, price: str, at: datetime = NOW,
) -> None:
    sell_intent = TradeIntent(
        "ti-2", "idem-2", "monitor-AAPL", asset(), Side.SELL, ExecutionMode.PAPER, "position_monitor", at,
        requested_quantity=Decimal(quantity),
    )
    await repositories.trade_intents.create_once("ti-2", sell_intent, status=sell_intent.status.value, unique_value=sell_intent.idempotency_key)
    sell_fill = Fill("fill-2", "ti-2", "order-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), Decimal("0"), Decimal("0"), at)
    await repositories.fills.create_once("fill-2", sell_fill, unique_value=None)
    sell_event = SettlementEvent("se-2", "fill-2", "ti-2", asset(), Side.SELL, ExecutionMode.PAPER, Decimal(quantity), Decimal(price), at)
    await repositories.settlements.create_once("se-2", sell_event, status=sell_event.status.value, unique_value="fill-2")


async def test_full_round_trip_produces_one_trade_attribution_with_correct_fields(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, stop_loss=Decimal("140"), target_price=Decimal("170"), opportunity_id="opp-1")
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    later = NOW + timedelta(hours=1)
    await _seed_sell(repositories, quantity="10", price="165", at=later)
    summary = await processor.process_pending()
    assert summary.completed == 1

    rows = await repositories.trade_attributions.list_all()
    assert len(rows) == 1
    attribution = hydrate("trade_attributions", rows[0]["payload"])
    assert attribution.asset == asset()
    assert attribution.lot_id
    assert attribution.opening_trade_intent_id == "ti-1"
    assert attribution.closing_trade_intent_id == "ti-2"
    assert attribution.closing_fill_id == "fill-2"
    assert attribution.quantity == Decimal("10")
    assert attribution.entry_price == Decimal("150")
    assert attribution.entry_at == NOW
    assert attribution.exit_price == Decimal("165")
    assert attribution.exit_at == later
    assert attribution.realized_pnl == Decimal("150")  # (165-150)*10
    assert attribution.exit_reason == "other"  # 165 is strictly between stop(140) and target(170)
    # Only two prices were ever observed for this lot -- the entry fill (150)
    # and the exit fill (165) -- so mfe/mae are exactly those two.
    assert attribution.max_favorable_excursion == Decimal("165")
    assert attribution.max_adverse_excursion == Decimal("150")
    assert attribution.entry_context["risk_snapshot"]["regime"] == "low_vol_bull"
    assert attribution.entry_context["opportunity_metadata"]["composite_score"] == "82"


async def test_partial_close_attributes_only_the_closed_portion(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, quantity="10", price="150")
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    await _seed_sell(repositories, quantity="4", price="160")
    summary = await processor.process_pending()
    assert summary.completed == 1

    rows = await repositories.trade_attributions.list_all()
    assert len(rows) == 1
    attribution = hydrate("trade_attributions", rows[0]["payload"])
    assert attribution.quantity == Decimal("4")
    assert attribution.realized_pnl == Decimal("40")  # (160-150)*4

    lot_rows = await repositories.position_lots.list_all()
    lot = hydrate("position_lots", lot_rows[0]["payload"])
    assert lot.status == "partially_closed"
    assert lot.remaining_quantity == Decimal("6")


async def test_single_closing_fill_that_fifo_closes_two_lots_produces_two_attribution_records(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    await _seed_buy(repositories, fill_id="fill-1", quantity="5", price="100")
    await processor.process_pending()

    later_open = NOW + timedelta(minutes=1)
    intent2 = TradeIntent("ti-1b", "idem-1b", "corr-1b", asset(), Side.BUY, ExecutionMode.PAPER, "manual", later_open, requested_quantity=Decimal("5"))
    await repositories.trade_intents.create_once("ti-1b", intent2, status=intent2.status.value, unique_value=intent2.idempotency_key)
    fill2 = Fill("fill-1b", "ti-1b", "order-1b", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("120"), Decimal("0"), Decimal("0"), later_open)
    await repositories.fills.create_once("fill-1b", fill2, unique_value=None)
    event2 = SettlementEvent("se-1b", "fill-1b", "ti-1b", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("120"), later_open)
    await repositories.settlements.create_once("se-1b", event2, status=event2.status.value, unique_value="fill-1b")
    await processor.process_pending()

    assert len(await repositories.position_lots.list_all()) == 2

    later_close = NOW + timedelta(minutes=2)
    await _seed_sell(repositories, quantity="10", price="150", at=later_close)
    summary = await processor.process_pending()
    assert summary.completed == 1

    rows = await repositories.trade_attributions.list_all()
    assert len(rows) == 2
    attributions = sorted((hydrate("trade_attributions", r["payload"]) for r in rows), key=lambda a: a.entry_price)
    assert attributions[0].entry_price == Decimal("100")
    assert attributions[0].realized_pnl == Decimal("250")  # (150-100)*5
    assert attributions[1].entry_price == Decimal("120")
    assert attributions[1].realized_pnl == Decimal("150")  # (150-120)*5


async def test_exit_reason_inferred_as_stop_loss_when_exit_at_or_beyond_stop(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, stop_loss=Decimal("140"), target_price=Decimal("170"))
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()
    await _seed_sell(repositories, quantity="10", price="138")  # at/below stop
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason == "stop_loss"


async def test_exit_reason_inferred_as_target_price_when_exit_at_or_beyond_target(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, stop_loss=Decimal("140"), target_price=Decimal("170"))
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()
    await _seed_sell(repositories, quantity="10", price="172")  # at/above target
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason == "target_price"


async def test_exit_reason_is_none_when_opening_intent_had_no_protective_levels(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, quantity="10", price="150")  # no stop_loss/target_price
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()
    await _seed_sell(repositories, quantity="10", price="160")
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason is None


async def test_project_attribution_is_idempotent_on_replay(tmp_path) -> None:
    """Simulates a crash-resume: replaying the same closing SettlementEvent
    through _project_attribution a second time must not create a duplicate
    record -- create_once's primary-key idempotency alone must be sufficient."""
    from tradepulse.settlement.engine import _project_attribution

    repositories = await _repositories(tmp_path)
    await _seed_buy(repositories, quantity="10", price="150")
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()
    await _seed_sell(repositories, quantity="10", price="160")
    await processor.process_pending()
    assert len(await repositories.trade_attributions.list_all()) == 1

    sell_event_row = await repositories.settlements.get("se-2")
    sell_event = hydrate("settlements", sell_event_row["payload"])
    await _project_attribution(repositories, sell_event)

    assert len(await repositories.trade_attributions.list_all()) == 1


# ---- Exit Intelligence -- exit_reason "time_stop" inference ---------------


async def test_infer_exit_reason_classifies_time_stop_when_held_past_max_hold_days(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, max_hold_days=5)  # no stop_loss/target_price -- isolate the time-stop path
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    exit_at = NOW + timedelta(days=5)  # exactly max_hold_days
    await _seed_sell(repositories, quantity="10", price="152", at=exit_at)  # a price that hits neither (nonexistent) stop nor target
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason == "time_stop"


async def test_infer_exit_reason_falls_back_to_other_when_held_days_below_max_hold_days(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, max_hold_days=5)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    exit_at = NOW + timedelta(days=4)  # one day short of max_hold_days
    await _seed_sell(repositories, quantity="10", price="152", at=exit_at)
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason == "other"


async def test_infer_exit_reason_price_based_reason_still_wins_over_time_stop_on_coincidental_tie(tmp_path) -> None:
    """A price-based reason is checked FIRST -- an exit that's both past
    max_hold_days AND at/beyond the stop must still classify as stop_loss,
    the unchanged tie-break precedent."""
    repositories = await _repositories(tmp_path)
    await _seed_buy_with_protective_levels(repositories, stop_loss=Decimal("140"), max_hold_days=5)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)
    await processor.process_pending()

    exit_at = NOW + timedelta(days=10)  # well past max_hold_days too
    await _seed_sell(repositories, quantity="10", price="138", at=exit_at)  # at/below stop
    await processor.process_pending()

    attribution = hydrate("trade_attributions", (await repositories.trade_attributions.list_all())[0]["payload"])
    assert attribution.exit_reason == "stop_loss"


async def test_current_stop_survives_settlement_rebuild_across_governing_lot_change(tmp_path) -> None:
    """Exit Intelligence's monotonic ratchet must be immune to the
    pre-existing PROTECTIVE_THRESHOLD_POLICY="first_entry" instability:
    when the OLDEST lot closes and a younger lot becomes governing (so
    stop_loss/target_price legitimately change), Holding.current_stop must
    be carried forward verbatim, unaffected."""
    repositories = await _repositories(tmp_path)
    processor = SettlementProcessor(repositories, _no_op_alerter(), clock=lambda: NOW)

    # Lot 1 (oldest, governing): stop_loss=90
    await _seed_buy_with_protective_levels(repositories, fill_id="fill-1", quantity="5", price="100", stop_loss=Decimal("90"))
    await processor.process_pending()

    # Lot 2 (younger, a scale-in): stop_loss=95 -- a distinct TradeIntent/Fill/SettlementEvent
    later_open = NOW + timedelta(minutes=1)
    intent2 = TradeIntent(
        "ti-1b", "idem-1b", "corr-1b", asset(), Side.BUY, ExecutionMode.PAPER, "ai_scan", later_open,
        requested_quantity=Decimal("5"), stop_loss=Decimal("95"),
    )
    await repositories.trade_intents.create_once("ti-1b", intent2, status=intent2.status.value, unique_value=intent2.idempotency_key)
    fill2 = Fill("fill-1b", "ti-1b", "order-1b", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("100"), Decimal("0"), Decimal("0"), later_open)
    await repositories.fills.create_once("fill-1b", fill2, unique_value=None)
    event2 = SettlementEvent("se-1b", "fill-1b", "ti-1b", asset(), Side.BUY, ExecutionMode.PAPER, Decimal("5"), Decimal("100"), later_open)
    await repositories.settlements.create_once("se-1b", event2, status=event2.status.value, unique_value="fill-1b")
    await processor.process_pending()

    holding_row = await repositories.holdings.get(asset_identity_key(asset()))
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.stop_loss == Decimal("90")  # lot 1 (oldest) still governs
    assert holding.current_stop is None  # nothing has ratcheted yet

    # Simulate the monitor having already ratcheted a stop this cycle.
    ratcheted = replace(holding, current_stop=Decimal("102"))
    await repositories.holdings.update(asset_identity_key(asset()), ratcheted)

    # Close lot 1 (the governing lot) fully -- FIFO closes oldest first.
    later_close = NOW + timedelta(minutes=2)
    await _seed_sell(repositories, quantity="5", price="105", at=later_close)
    summary = await processor.process_pending()
    assert summary.completed == 1

    holding_row = await repositories.holdings.get(asset_identity_key(asset()))
    holding = hydrate("holdings", holding_row["payload"])
    assert holding.stop_loss == Decimal("95")  # governing lot changed to lot 2 -- proves the edge case is real, not a no-op
    assert holding.current_stop == Decimal("102")  # carried forward unchanged despite the rebuild
