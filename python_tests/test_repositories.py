import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradepulse.models import AIResponse, AssetClass, AssetIdentity, Holding, PositionLot
from tradepulse.persistence import AsyncSQLiteDatabase, PersistenceRepositories, hydrate


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
