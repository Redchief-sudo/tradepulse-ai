import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradepulse.models import (
    AIResponse,
    AssetClass,
    AssetIdentity,
    ExecutionMode,
    Fill,
    Holding,
    PortfolioSnapshot,
    PositionLot,
    Side,
    TradeIntent,
    TradeIntentStatus,
)
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, RepositoryPaginationError, hydrate
from tradepulse.persistence.repositories import _paginate_all, list_all_by_json_time_range, list_all_by_statuses


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def _repositories(tmp_path) -> PersistenceRepositories:
    database = AsyncSQLiteDatabase(f"sqlite:///{tmp_path}/test.db")
    await database.initialize()
    return PersistenceRepositories.create(database)


async def test_list_all_returns_every_row_for_non_status_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    holding = Holding(asset, Decimal("10"), Decimal("150"), NOW)
    assert await repositories.holdings.create_once("holding-1", holding)

    rows = await repositories.holdings.list_all()
    assert len(rows) == 1
    assert hydrate("holdings", rows[0]["payload"]) == holding


async def test_list_all_respects_limit_bounds(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        await repositories.holdings.list_all(limit=0)


async def test_list_all_orders_by_created_at_ascending(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    for i in range(3):
        await repositories.holdings.create_once(f"holding-{i}", Holding(asset, Decimal(i + 1), Decimal("150"), NOW))
    rows = await repositories.holdings.list_all()
    assert [row["record_id"] for row in rows] == ["holding-0", "holding-1", "holding-2"]


async def test_list_recent_orders_by_created_at_descending(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    for i in range(3):
        await repositories.holdings.create_once(f"holding-{i}", Holding(asset, Decimal(i + 1), Decimal("150"), NOW))
    rows = await repositories.holdings.list_recent()
    assert [row["record_id"] for row in rows] == ["holding-2", "holding-1", "holding-0"]  # newest first, reverse of list_all


async def test_list_recent_respects_limit_bounds(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        await repositories.holdings.list_recent(limit=0)
    with pytest.raises(ValueError, match="limit"):
        await repositories.holdings.list_recent(limit=1001)


async def test_list_recent_respects_limit_count(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    for i in range(5):
        await repositories.holdings.create_once(f"holding-{i}", Holding(asset, Decimal(i + 1), Decimal("150"), NOW))
    rows = await repositories.holdings.list_recent(limit=2)
    assert [row["record_id"] for row in rows] == ["holding-4", "holding-3"]


async def test_delete_is_permitted_on_holdings(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    asset = AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")
    await repositories.holdings.create_once("holding-1", Holding(asset, Decimal("10"), Decimal("150"), NOW))

    assert await repositories.holdings.delete("holding-1") is True
    assert await repositories.holdings.get("holding-1") is None


async def test_delete_is_rejected_on_append_only_tables(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.fills.delete("fill-1")
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.settlements.delete("settlement-1")
    with pytest.raises(ValueError, match="delete is not permitted"):
        await repositories.trade_intents.delete("intent-1")


def _lot(lot_id: str = "lot-1", **overrides) -> PositionLot:
    defaults = dict(
        lot_id=lot_id, originating_fill_id=f"fill-{lot_id}",
        asset=AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL"),
        position_side="long", opened_quantity=Decimal("10"), remaining_quantity=Decimal("10"),
        acquisition_price=Decimal("150"), opened_at=NOW,
    )
    defaults.update(overrides)
    return PositionLot(**defaults)


async def test_mutate_happy_path_updates_and_returns_new_value(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    lot = _lot()
    await repositories.position_lots.create_once("lot-1", lot, unique_value=lot.originating_fill_id)

    result = await repositories.position_lots.mutate("lot-1", lambda current: replace(current, mfe_price=Decimal("160")))

    assert result is not None
    assert result.mfe_price == Decimal("160")
    row = await repositories.position_lots.get("lot-1")
    assert hydrate("position_lots", row["payload"]).mfe_price == Decimal("160")


async def test_mutate_returns_none_for_missing_record(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    result = await repositories.position_lots.mutate("nonexistent", lambda current: replace(current, mfe_price=Decimal("1")))
    assert result is None


async def test_mutate_leaves_row_unchanged_when_decide_returns_none(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    lot = _lot()
    await repositories.position_lots.create_once("lot-1", lot, unique_value=lot.originating_fill_id)

    result = await repositories.position_lots.mutate("lot-1", lambda current: None)

    assert result is None
    row = await repositories.position_lots.get("lot-1")
    assert hydrate("position_lots", row["payload"]) == lot


async def test_mutate_rejects_a_non_updatable_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="mutate requires an updatable current-state table"):
        await repositories.ai_responses.mutate("req-1", lambda current: current)


async def test_mutate_serializes_concurrent_writers_without_losing_either_update(tmp_path) -> None:
    """Two concurrent mutate() calls on the SAME row must both land -- the
    property a plain get()-then-update() pair would NOT have (a lost-update
    race), which is exactly why mutate() exists (see Outcome Attribution:
    position_lots gained a second writer, the position monitor, running
    concurrently with settlement)."""
    repositories = await _repositories(tmp_path)
    lot = _lot(realized_pnl=Decimal("0"))
    await repositories.position_lots.create_once("lot-1", lot, unique_value=lot.originating_fill_id)

    def _increment(current: PositionLot) -> PositionLot:
        return replace(current, realized_pnl=current.realized_pnl + Decimal("1"))

    await asyncio.gather(
        repositories.position_lots.mutate("lot-1", _increment),
        repositories.position_lots.mutate("lot-1", _increment),
    )

    row = await repositories.position_lots.get("lot-1")
    result = hydrate("position_lots", row["payload"])
    assert result.realized_pnl == Decimal("2")  # both increments landed -- neither was lost to the other


async def test_ai_response_persists_for_audit_trail(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    response = AIResponse(
        request_id="req-1", provider="anthropic", model="claude-haiku-4-5", schema_version="1.0",
        completed_at=NOW, result={"candidates": []}, latency_ms=250,
    )
    assert await repositories.ai_responses.create_once("req-1", response)

    row = await repositories.ai_responses.get("req-1")
    assert row is not None
    assert hydrate("ai_responses", row["payload"]) == response

    # A retried scan with the same request_id must not duplicate the audit row.
    assert await repositories.ai_responses.create_once("req-1", response) is False


def _asset() -> AssetIdentity:
    return AssetIdentity("AAPL", AssetClass.EQUITY, "alpaca:AAPL")


def _intent(i: int, status: TradeIntentStatus) -> TradeIntent:
    return TradeIntent(
        f"ti-{i}", f"idem-{i}", f"corr-{i}", _asset(), Side.BUY, ExecutionMode.PAPER, "manual", NOW,
        requested_quantity=Decimal("5"), status=status,
    )


async def _seed_intent(repositories: PersistenceRepositories, i: int, status: TradeIntentStatus) -> None:
    intent = _intent(i, status)
    await repositories.trade_intents.create_once(f"ti-{i}", intent, status=status.value, unique_value=intent.idempotency_key)


async def test_list_by_statuses_matches_any_status_in_the_set(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, 1, TradeIntentStatus.ACCEPTED)
    await _seed_intent(repositories, 2, TradeIntentStatus.FILLED)
    await _seed_intent(repositories, 3, TradeIntentStatus.SUBMITTED)

    rows = await repositories.trade_intents.list_by_statuses([TradeIntentStatus.ACCEPTED.value, TradeIntentStatus.SUBMITTED.value])

    assert {row["record_id"] for row in rows} == {"ti-1", "ti-3"}


async def test_list_by_statuses_rejects_non_status_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="has no status"):
        await repositories.holdings.list_by_statuses(["accepted"])


async def test_list_by_statuses_rejects_empty_statuses(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="statuses must not be empty"):
        await repositories.trade_intents.list_by_statuses([])


async def test_list_by_statuses_cursor_pages_without_skip_or_repeat(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    for i in range(10):
        await _seed_intent(repositories, i, TradeIntentStatus.ACCEPTED)

    seen: list[str] = []
    after = None
    while True:
        page = await repositories.trade_intents.list_by_statuses([TradeIntentStatus.ACCEPTED.value], limit=3, after=after)
        if not page:
            break
        seen.extend(row["record_id"] for row in page)
        after = (page[-1]["created_at"], page[-1]["record_id"])

    assert seen == [f"ti-{i}" for i in range(10)]  # every row exactly once, in order, across 4 pages


async def test_list_all_by_statuses_fetches_a_set_spanning_several_batches(tmp_path) -> None:
    """Direct proof there's no remaining row-count ceiling -- a seeded set
    much larger than one batch is returned completely, not truncated."""
    repositories = await _repositories(tmp_path)
    await asyncio.gather(*(_seed_intent(repositories, i, TradeIntentStatus.ACCEPTED) for i in range(25)))

    rows = await list_all_by_statuses(repositories.trade_intents, [TradeIntentStatus.ACCEPTED.value], batch_size=4)

    assert {row["record_id"] for row in rows} == {f"ti-{i}" for i in range(25)}


async def test_paginate_all_raises_on_a_stalled_cursor() -> None:
    """A pagination cursor that fails to advance must raise, never return a
    silently partial result -- the core correctness contract this whole
    pagination layer exists to guarantee. A FULL batch (== batch_size) is
    needed to force a second page fetch; the stall itself is the second
    call returning the identical trailing cursor as the first."""
    batch_size = 3
    stall_batch = [{"created_at": "2026-01-01T00:00:00+00:00", "record_id": f"r-{i}", "payload": {}} for i in range(batch_size)]

    async def fetch_page(after, limit):
        return stall_batch  # always the SAME full page/cursor -- simulates a stalled cursor

    with pytest.raises(RepositoryPaginationError, match="failed to advance"):
        await _paginate_all(fetch_page, batch_size=batch_size)


async def test_exists_with_status_and_asset_matches_full_identity(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, 1, TradeIntentStatus.ACCEPTED)

    assert await repositories.trade_intents.exists_with_status_and_asset([TradeIntentStatus.ACCEPTED.value], _asset()) is True


async def test_exists_with_status_and_asset_false_when_status_does_not_match(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, 1, TradeIntentStatus.FILLED)

    assert await repositories.trade_intents.exists_with_status_and_asset([TradeIntentStatus.ACCEPTED.value], _asset()) is False


async def test_exists_with_status_and_asset_false_for_a_different_venue(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await _seed_intent(repositories, 1, TradeIntentStatus.ACCEPTED)
    other_venue = AssetIdentity("AAPL", AssetClass.EQUITY, "otherbroker:AAPL", venue="other-venue")

    assert await repositories.trade_intents.exists_with_status_and_asset([TradeIntentStatus.ACCEPTED.value], other_venue) is False


async def test_exists_with_status_and_asset_rejects_non_trade_intents_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="only defined for trade_intents"):
        await repositories.orders.exists_with_status_and_asset(["accepted"], _asset())


def _snapshot(snapshot_id: str, total_equity: Decimal) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id=snapshot_id, as_of=NOW, total_equity=total_equity, cash_balance=Decimal("0"),
        holdings_value=Decimal("0"), sector_exposure={}, open_positions=0, outstanding_orders=0,
        trades_today=0, daily_pnl_pct=Decimal("0"), source="broker",
    )


async def test_max_by_json_field_finds_the_largest_value_regardless_of_insertion_order(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    await repositories.equity_snapshots.create_once("snap-1", _snapshot("snap-1", Decimal("50000")))
    await repositories.equity_snapshots.create_once("snap-2", _snapshot("snap-2", Decimal("120000")))
    await repositories.equity_snapshots.create_once("snap-3", _snapshot("snap-3", Decimal("80000")))

    row = await repositories.equity_snapshots.max_by_json_field("total_equity")

    assert row["record_id"] == "snap-2"


async def test_max_by_json_field_returns_the_exact_decimal_not_a_lossy_cast(tmp_path) -> None:
    """The sort itself uses a REAL cast, but the returned value must be the
    ORIGINAL Decimal string -- proven with two values close enough that a
    float round-trip could plausibly blur them."""
    repositories = await _repositories(tmp_path)
    await repositories.equity_snapshots.create_once("snap-1", _snapshot("snap-1", Decimal("100000.01")))
    await repositories.equity_snapshots.create_once("snap-2", _snapshot("snap-2", Decimal("100000.011")))

    row = await repositories.equity_snapshots.max_by_json_field("total_equity")

    assert row["record_id"] == "snap-2"
    assert Decimal(str(row["payload"]["total_equity"])) == Decimal("100000.011")


async def test_max_by_json_field_returns_none_on_an_empty_table(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    assert await repositories.equity_snapshots.max_by_json_field("total_equity") is None


async def test_max_by_json_field_rejects_a_malformed_field_name(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="invalid JSON field name"):
        await repositories.equity_snapshots.max_by_json_field("total_equity'; DROP TABLE equity_snapshots; --")


def _fill(fill_id: str, filled_at: datetime) -> Fill:
    return Fill(
        fill_id, "ti-1", "order-1", _asset(), Side.BUY, ExecutionMode.PAPER,
        Decimal("1"), Decimal("100"), Decimal("0"), Decimal("0"), filled_at,
    )


async def test_list_by_json_time_range_only_returns_rows_inside_the_window(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    start = NOW
    end = NOW + timedelta(hours=1)
    await repositories.fills.create_once("fill-before", _fill("fill-before", start - timedelta(seconds=1)), unique_value=None)
    await repositories.fills.create_once("fill-in", _fill("fill-in", start + timedelta(minutes=30)), unique_value=None)
    await repositories.fills.create_once("fill-at-start", _fill("fill-at-start", start), unique_value=None)
    await repositories.fills.create_once("fill-at-end", _fill("fill-at-end", end), unique_value=None)  # end is exclusive

    rows = await repositories.fills.list_by_json_time_range("filled_at", start, end)

    assert {row["record_id"] for row in rows} == {"fill-in", "fill-at-start"}


async def test_list_all_by_json_time_range_fetches_a_window_spanning_several_batches(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    start = NOW
    end = NOW + timedelta(hours=1)
    await asyncio.gather(*(
        repositories.fills.create_once(f"fill-{i}", _fill(f"fill-{i}", start + timedelta(minutes=i)), unique_value=None)
        for i in range(25)
    ))

    rows = await list_all_by_json_time_range(repositories.fills, "filled_at", start, end, batch_size=4)

    assert {row["record_id"] for row in rows} == {f"fill-{i}" for i in range(25)}


async def test_list_by_json_time_range_rejects_a_malformed_field_name(tmp_path) -> None:
    repositories = await _repositories(tmp_path)
    with pytest.raises(ValueError, match="invalid JSON field name"):
        await repositories.fills.list_by_json_time_range("filled_at'; --", NOW, NOW + timedelta(hours=1))
